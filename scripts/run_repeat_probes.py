"""Compare one fresh question with and without exploratory caption retrieval."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time

import torch
from run_overnight_hits import INPUTS, OLD_BASELINE, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared

OUT = ROOT / 'output/agentir-repeat-probes-36'
ARMS = ('fresh', 'probe')
ASK = """The dialogue has repeated information. Ask ONE short, visually answerable question
that would distinguish plausible scenes. Use the caption examples to find a difference
in setting, activity, relation, or appearance the user has not established. Examples are
possible scenes only. Phrase the question openly so the answer can contradict an example.
Return just the question ending in a question mark."""
PLAN = """Write two different short image search descriptions, one per line. Preserve
the main subject and facts given by the user. Explore two plausible alternatives for
one unresolved visual detail that could distinguish the scene. These are search
hypotheses. Output only the two descriptions, with no introduction or explanation."""


def normalized(text):
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def trigger(turns):
    seen = set()
    for after, turn in enumerate(turns[:-1], 1):
        pair = tuple(normalized(turn[key]) for key in ('question', 'answer'))
        if pair in seen:
            return after
        seen.add(pair)


def prepare():
    cases, ids, suffix_seconds, suffix_turns = [], set(), 0, 0
    for directory in (OLD_BASELINE, OVERNIGHT_OUTPUT):
        for file in sorted((directory/'vanilla/dialogues').glob('*.json')):
            raw = file.read_bytes()
            record = json.loads(raw)
            qid = record['qid']
            assert qid not in ids and record['hits_only']
            ids.add(qid)
            start = trigger(record['turns'])
            if start is None:
                continue
            assert min(record['ranks'][:start+1]) > 10
            target_dir = INPUTS/'targets' if directory == OLD_BASELINE else REPO/'data'
            assert (target_dir/record['target']).is_file()
            cases.append(dict(qid=qid, start=start, source=str(file.relative_to(ROOT)),
                              sha256=hashlib.sha256(raw).hexdigest(), target_dir=str(target_dir.relative_to(ROOT))))
            suffix = record['turns'][start:]
            suffix_turns += len(suffix)
            suffix_seconds += sum(t['seconds'] for t in suffix)
    assert len(ids) == 500 and len(cases) == 36
    return dict(cases=sorted(cases, key=lambda c:c['qid']), vanilla_suffix_turns=suffix_turns,
                vanilla_suffix_minutes=round(suffix_seconds/60, 1),
                max_turns_per_arm=sum(10-c['start'] for c in cases),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def complete(worker, instruction, payload, tokens, seed):
    return worker.complete([dict(role='system', content=instruction),
                            dict(role='user', content=json.dumps(payload))], tokens, seed=seed, temperature=0)


def intervention(pipe, arm, history, evidence, seed, fallback):
    payload = dict(dialogue=history, candidate_captions=evidence)
    probes = []
    if arm == 'probe':
        raw = complete(pipe.client, PLAN, payload, 128, seed)
        lines = [re.sub(r'^\s*(?:[-*]|\d+[.)])\s*', '', line).strip() for line in raw.splitlines() if line.strip()]
        lines = list(dict.fromkeys(lines))
        for query in lines if len(lines) == 2 else []:
            scores = torch.nn.functional.normalize(pipe.get_text_features(query), dim=-1) @ pipe.img_embs.T
            ids = scores.flatten().topk(3).indices.tolist()
            probes.append(dict(query=query, ids=ids, captions=[pipe.captions[i]['caption'][0] for i in ids]))
        if probes:
            payload['additional_candidate_captions'] = [p['captions'] for p in probes]
    question = complete(pipe.client, ASK, payload, 72, seed+1).strip().strip('"').strip()
    valid = question.endswith('?') and question.count('?') == 1 and '\n' not in question
    return question if valid else fallback, dict(searches=probes, raw_plan=raw if arm=='probe' else None,
                                                generated=question, fallback=not valid)


@torch.inference_mode()
def continuation(pipe, arm, case, record, target):
    start = case['start']
    dialogue = [record['queries'][0]] + [f"{t['question']} {t['answer']}" for t in record['turns'][:start]]
    ranks = record['ranks'][:start+1]
    query = record['queries'][start]
    turns = []
    started = time.perf_counter()
    for r in range(start, 10):
        seed = 1021 + 100*case['qid'] + r
        torch.manual_seed(seed)
        if r == start:
            saved = record['turns'][r]
            assert saved['query_before'] == query
            question, debug = intervention(pipe, arm, dialogue, saved['selected_captions'], seed, saved['question'])
        else:
            ids = pipe.cluster_representatives(order[:500-50*r].flip(0))
            evidence = [pipe.captions[i]['caption'][0] for i in ids]
            candidates = shared.question_candidates(pipe, dialogue, query, evidence)
            question = pipe.select_question(query, candidates, scores, 10, pipe.img_embs, 500, r+1)
            debug = {}
        answer = pipe.client.answer(ROOT/case['target_dir']/record['target'], question)
        dialogue.append(f'{question} {answer}')
        updated = pipe.reconstruct_dialog(dialogue)
        if updated == query:
            updated = pipe.paraphrase(updated)
        turns.append(dict(round=r+1, query_before=query, question=question, answer=answer,
                          query_after=updated, **debug))
        query = updated
        scores = (torch.nn.functional.normalize(pipe.get_text_features(query), dim=-1) @ pipe.img_embs.T).flatten()
        order = scores.argsort().flip(0)
        ranks.append(int(shared.inverse_ranks(order)[target]))
        if ranks[-1] <= 10:
            break
    return dict(qid=case['qid'], arm=arm, after_round=start, ranks=ranks, continuation=turns,
                seconds=round(time.perf_counter()-started, 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = prepare()
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path) != plan:
            raise ValueError('Plan or code differs from saved experiment')
        shared.atomic_json(path, plan)
        print(f"36 paired states; saved vanilla continuations used {plan['vanilla_suffix_turns']} turns / {plan['vanilla_suffix_minutes']} minutes per arm; maximum {plan['max_turns_per_arm']} turns per arm.", flush=True)
        if args.prepare_only:
            return
        if not torch.backends.mps.is_available():
            raise SystemExit('Metal unavailable here. Start this runner in macOS Terminal.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {name:i for i,name in enumerate(pipe.search_space)}
            metrics = {arm:dict(completed=0, hits=0, gained=0, lost=0) for arm in ARMS}
            comparison = dict(probe_only=0, fresh_only=0)
            for i,case in enumerate(plan['cases'], 1):
                record = shared.read_json(ROOT/case['source'])
                vanilla = min(record['ranks']) <= 10
                hits = {}
                for arm in ARMS:
                    file = OUT/arm/f"{case['qid']}.json"
                    if not file.exists():
                        result = continuation(pipe, arm, case, record, positions[record['target']])
                        shared.atomic_json(file, result)
                    result = shared.read_json(file)
                    hit = hits[arm] = min(result['ranks']) <= 10
                    m = metrics[arm]
                    m['completed'] += 1
                    m['hits'] += hit
                    m['gained'] += hit and not vanilla
                    m['lost'] += vanilla and not hit
                    print(f"{i}/36 {arm} qid {case['qid']} hit={hit} vanilla={vanilla}", flush=True)
                comparison['probe_only'] += hits['probe'] and not hits['fresh']
                comparison['fresh_only'] += hits['fresh'] and not hits['probe']
                shared.atomic_json(OUT/'summary.json', dict(arms=metrics, paired=comparison, complete=i==36))
        finally:
            worker.close()


if __name__ == '__main__':
    main()
