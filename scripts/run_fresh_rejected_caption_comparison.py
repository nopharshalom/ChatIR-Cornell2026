"""Validate rejected-caption question generation against the usual prompt on new initial misses."""
import fcntl
import random
import subprocess
import sys
import tempfile
import time

import torch

from run_five_question_probes import REVISION, candidate_ids, compare_questions, git
from run_probe_pool_sweep import cpu_retriever
from run_rejected_caption_pilot import INPUTS, REPO, ROOT, SNAPSHOT, Search, generator, mac, score_outcome, shared

OUT = ROOT / 'output/agentir-fresh-rejected-caption-probes-100'
SEED = 20261118


def prepare(data, positions):
    path = OUT / 'plan.json'
    if path.exists():
        return shared.read_json(path)
    known = set()
    for pattern in ('**/dialogues/*.json', '**/cases/*.json'):
        known.update(int(f.stem) for f in (ROOT / 'output').glob(pattern) if f.stem.isdigit())
    used_images = {data[i]['img'] for i in known if i < len(data)}
    available = [i for i in candidate_ids(data, known) if data[i]['img'] in positions
                 and data[i]['img'] not in used_images and (REPO / 'data' / data[i]['img']).is_file()]
    random.Random(SEED).shuffle(available)
    torch.set_num_threads(4)
    score = cpu_retriever()
    ids = []
    screened = 0
    for start in range(0, len(available), 8):
        batch = available[start:start+8]
        scores = score([data[i]['dialog'][0] for i in batch])
        for qid, top in zip(batch, scores.topk(10, dim=1).indices.tolist()):
            screened += 1
            image = data[qid]['img']
            if positions[image] in top or image in used_images:
                continue
            ids.append(qid)
            used_images.add(image)
            if len(ids) == 100:
                plan = dict(qids=ids, screened=screened, sampling_seed=SEED,
                            excluded_prior_records=len(known), snapshot=SNAPSHOT.name, revision=REVISION)
                shared.atomic_json(path, plan)
                return plan
    raise RuntimeError(f'Only {len(ids)} eligible misses')


def run_case(pipe, task, qid, target):
    initial = task['dialog'][0]
    search = Search(pipe)
    scores = search.similarity([initial], 'initial')[0]
    order = scores.argsort().flip(0)
    rank = int(shared.inverse_ranks(order)[target])
    assert rank > 10, f'CPU and Metal disagree on eligibility for {qid}'
    shown = order[:10]
    torch.manual_seed(1021 + 100 * qid)
    reps = pipe.cluster_representatives(order[:500].flip(0))
    evidence = [pipe.captions[i]['caption'][0] for i in reps]
    rejected = [pipe.captions[i]['caption'][0] for i in shown.tolist()]
    arms = {}
    for name, source in [('usual', pipe), ('rejected', generator(pipe, rejected))]:
        pipe.client.next_seed = 1021 + qid
        questions = shared.question_candidates(source, [initial], initial, evidence)
        choices, details = compare_questions(pipe, Search(pipe), initial, evidence, questions, qid)
        for p in details['answer_paths']:
            if p.get('score') == float('-inf'):
                p['score'] = None
        arms[name] = dict(candidates=questions, question=choices['probe'], probe_details=details)
    outcomes = {}
    for arm in arms.values():
        q = arm['question']
        if q not in outcomes:
            answer = pipe.client.answer(REPO / 'data' / task['img'], q)
            rewrite = pipe.reconstruct_dialog([initial, f'{q} {answer}'])
            if rewrite == initial:
                rewrite = pipe.paraphrase(rewrite)
            outcomes[q] = score_outcome(search, dict(answer=answer, rewrite=rewrite), target, shown)
        arm.update(outcomes[q])
    return dict(qid=qid, initial=initial, initial_rank=rank, displayed=shown.tolist(), evidence=evidence,
                rejected_captions=rejected, **arms)


@torch.inference_mode()
def run():
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    positions = {name: i for i, name in enumerate(shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json'))}
    plan = prepare(data, positions)
    ids = plan['qids']
    old = set(shared.read_json(ROOT / 'output/agentir-hundred-probe/summary.json')['cases'])
    assert len(ids) == len(set(ids)) == 100 and not old.intersection(ids)
    assert not {data[i]['img'] for i in ids}.intersection(data[i]['img'] for i in old)
    print(f"Prepared 100 separate initial misses from {plan['screened']} screened dialogues.", flush=True)
    rows, pending = [], []
    for i in ids:
        path = OUT / 'cases' / f'{i}.json'
        if path.exists():
            row = shared.read_json(path)
            assert row['qid'] == i and row['initial'] == data[i]['dialog'][0]
            rows.append(row)
        else:
            pending.append(i)
    if pending:
        if not torch.backends.mps.is_available():
            raise SystemExit('Sample ready. Continue in macOS Terminal for Metal access.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            for i in pending:
                started = time.monotonic()
                row = run_case(pipe, data[i], i, positions[data[i]['img']])
                shared.atomic_json(OUT / 'cases' / f'{i}.json', row)
                rows.append(row)
                print(f"{len(rows)}/100 · QID {i} · usual {row['usual']['raw_rank']} · "
                      f"rejected {row['rejected']['raw_rank']} · {time.monotonic()-started:.1f}s", flush=True)
        finally:
            worker.close()
    summary = dict(n=len(rows))
    for metric in ('raw_rank', 'unseen_rank'):
        pairs = [(r['usual'][metric] <= 10, r['rejected'][metric] <= 10) for r in rows]
        summary[metric] = dict(usual=sum(a for a,b in pairs), rejected=sum(b for a,b in pairs),
                               gained=sum(b and not a for a,b in pairs), lost=sum(a and not b for a,b in pairs))
    shared.atomic_json(OUT / 'summary.json', summary)
    print(summary)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock, tempfile.TemporaryDirectory() as source:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['tar', '-xf', '-', '-C', source], input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        run()
