"""Replay question selection with all questions about one photo in a single VQA call."""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
import fcntl
import json
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

import run_proxy_retrieval as proxy
from check_indexed_visual_answers import unique_object
from guard_history_veto import guard_metal_timeout
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, mac, shared

OUT = ROOT/'output/batched-proxy-replay'
PROMPT = ('Answer each numbered question independently using only this photo. Be brief. '
          'If the photo does not establish an answer, say "unsure". '
          'Return one JSON object mapping each question number to its answer string.')


def parse_batch(raw, n):
    text = raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:
        values = json.loads(text, object_pairs_hook=unique_object)
    except ValueError:
        return None
    keys = [str(i+1) for i in range(n)]
    if not isinstance(values, dict) or set(values) != set(keys):
        return None
    if any(not isinstance(values[k], str) or not values[k].strip() for k in keys):
        return None
    return [values[k].strip() for k in keys]


def cases():
    corpus = shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json')
    source_plan = shared.read_json(proxy.OUT/'plan.json')
    if source_plan['model'] != SNAPSHOT.name or len(corpus) != 50000:
        raise ValueError('The replay model or retrieval corpus differs from the pilot')
    result = []
    for arm in proxy.ARMS:
        rows = shared.read_records(proxy.OUT, arm)
        ordinal = 0
        for i, row in sorted(rows.items()):
            history = [row['queries'][0]]
            for j,(t,a) in enumerate(zip(row['turns'], row['proxy_choices'], strict=True),1):
                questions = list(dict.fromkeys(t['candidate_questions']))
                ids = a['image_ids']
                if (len(set(ids)) != 10 or a['ranks'] != proxy.sample_ranks(i,j) or
                        a['chosen'] != t['question'] or a['incumbent'] not in questions or
                        any(not (REPO/'data'/corpus[k]).is_file() for k in ids)):
                    raise ValueError(f'Mismatched proxy inputs for {arm}/{i}/{j}')
                query = t['query_before']
                scores = {}
                for question in questions if len(questions)>1 else []:
                    key = proxy.digest([history,query,question,ids])
                    branch = shared.read_json(proxy.OUT/'branch-cache'/f'{key}.json')
                    if [b['image_id'] for b in branch] != ids:
                        raise ValueError('Archived proxy order differs')
                    scores[question] = sum(b['hit'] for b in branch)
                if scores != a['scores']:
                    raise ValueError('Archived retrieval scores differ')
                scored = len(questions)>1
                result.append(dict(key=f'{arm}-{i}-{j}', arm=arm, history=history.copy(),
                                   query=query, questions=questions, images=ids, ranks=a['ranks'], round=j,
                                   incumbent=a['incumbent'], original=a['chosen'], original_scores=scores,
                                   timed=scored and ordinal%5==0, single_first=(ordinal//5)%2==0))
                ordinal += int(scored)
                history.append(f'{t["question"]} {t["answer"]}')
    return result


def evaluate(pipe, case, batch, current):
    clock = perf_counter()
    qs, ids = case['questions'], case['images']
    matrix = [[] for _ in qs]
    invalid = []
    calls = pipe.client.calls
    for image in ids if len(qs)>1 else []:
        photo = REPO/'data'/pipe.search_space[image]
        answers = None
        if batch:
            pipe.client.next_seed = proxy.SEED + image
            raw = pipe.client.complete([dict(role='system',content=PROMPT), dict(role='user',
                content=json.dumps({str(i+1):q for i,q in enumerate(qs)}))], 48*len(qs), image=photo)
            answers = parse_batch(raw,len(qs))
            if answers is None:
                invalid.append(dict(image=image,raw=raw))
        if answers is None:
            answers = []
            for question in qs:
                pipe.client.next_seed = proxy.SEED + image
                answers.append(pipe.client.answer(photo,question))
        for answers_for_q,answer in zip(matrix,answers,strict=True):
            answers_for_q.append(answer)
    visual_calls = pipe.client.calls-calls
    scores = {}
    for q,answers in zip(qs,matrix,strict=True):
        if not answers:
            continue
        top = {}
        for answer in dict.fromkeys(answers):
            updated = pipe.reconstruct_dialog([*case['history'],f'{q} {answer}'])
            if updated == case['query']:
                pipe.client.next_seed = proxy.SEED
                updated = pipe.paraphrase(updated)
            features = normalize(pipe.get_text_features(updated),dim=-1)
            top[answer] = (features @ pipe.img_embs.T).flatten().argsort().flip(0)[:10].tolist()
        scores[q] = sum(image in top[answer] for image,answer in zip(ids,answers,strict=True))
    chosen = case['incumbent']
    if scores:
        best = [q for q in qs if scores[q] == max(scores.values())]
        if chosen not in best:
            chosen = best[0] if len(best)==1 else pipe.select_question(
                case['query'],best,current,10,pipe.img_embs,500,case['round'])
    return dict(choice=chosen,scores=scores,answers=matrix,invalid_batches=invalid,
                visual_calls=visual_calls,total_calls=pipe.client.calls-calls,seconds=perf_counter()-clock)


def summarize(rows):
    timings = [r for r in rows if 'single' in r]
    seconds = {mode:sum(r[mode]['seconds'] for r in timings) for mode in ('batch','single')}
    scored = [r for r in rows if r['batch']['scores']]
    # Ratios use identical turns; original full-dialogue time includes unrelated work.
    return dict(turns=len(rows), scored=len(scored),
                same_choice=sum(r['batch']['choice']==r['original'] for r in rows),
                same_choice_scored=sum(r['batch']['choice']==r['original'] for r in scored),
                original_overrides=sum(r['original']!=r['incumbent'] for r in rows),
                preserved_overrides=sum(r['batch']['choice']==r['original']!=r['incumbent'] for r in rows),
                inferred_batches=10*len(scored),invalid_batches=sum(len(r['batch']['invalid_batches']) for r in rows),
                batch_visual_calls=sum(r['batch']['visual_calls'] for r in rows),
                paired_timing_turns=len(timings),mean_selector_seconds={k:v/len(timings) for k,v in seconds.items()} if timings else {},
                paired_speedup=seconds['single']/seconds['batch'] if seconds['batch'] else None,
                single_choice_matches_archive=sum(r['single']['choice']==r['original'] for r in timings))


@torch.inference_mode()
def run(check):
    data = cases()
    counts = Counter(c['arm'] for c in data)
    if sorted(counts.values()) != [23,29]:
        raise ValueError(f'Expected the agreed 52 archived turns, found {counts}')
    plan = dict(input_digest=proxy.digest(data), cases=[c['key'] for c in data],
                benchmark=[c['key'] for c in data if c['timed']], model=SNAPSHOT.name,
                source=proxy.hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    print(f'Verified {len(data)} frozen turns, {sum(len(c["questions"])>1 for c in data)} scored, '
          f'{len(plan["benchmark"])} paired timing comparisons.',flush=True)
    if check:
        return
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        lock = stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path)!=plan:
            raise ValueError('Replay checkpoint plan differs')
        shared.atomic_json(path,plan)
        pipe = None
        rows = []
        for c in data:
            dest = OUT/'turns'/f'{c["key"]}.json'
            if dest.exists():
                r = shared.read_json(dest)
                if r['key'] != c['key'] or r['original']!=c['original'] or ('single' in r)!=c['timed']:
                    raise ValueError(f'Invalid checkpoint {dest}')
            else:
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Launch the queue in Mac Terminal for Metal access.')
                    with patch.object(mac,'__file__',str(GRID_WORKER)):
                        worker = stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                    pipe = mac.load_blip(INPUTS,worker)
                current = (normalize(pipe.get_text_features(c['query']),dim=-1) @ pipe.img_embs.T).flatten()
                order = current.argsort().flip(0)
                if [int(order[j-1]) for j in c['ranks']]!=c['images']:
                    raise ValueError(f'Retrieval differs from saved pilot on {c["key"]}')
                r = {k:c[k] for k in ('key','arm','original','incumbent')}
                modes = ['batch']
                if c['timed']:
                    modes = ['single','batch'] if c['single_first'] else ['batch','single']
                for mode in modes:
                    r[mode] = evaluate(pipe,c,mode=='batch',current)
                shared.atomic_json(dest,r)
            rows.append(r)
            result = dict(complete=len(rows)==len(data),all=summarize(rows),arms={
                arm:summarize([r for r in rows if r['arm']==arm]) for arm in proxy.ARMS})
            shared.atomic_json(OUT/'summary.json',result)
            print(f'{len(rows)}/{len(data)} replay turns saved',flush=True)
        print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
