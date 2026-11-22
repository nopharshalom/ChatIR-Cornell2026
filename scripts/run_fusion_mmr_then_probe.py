"""Matched fusion/MMR evaluation, then a caption-weighted first-question probe pilot."""
import fcntl
import json
import torch

from run_overnight_hits import INPUTS, OLD_BASELINE, ROOT, SNAPSHOT, mac, shared
from run_five_question_probes import Search, REPO

OUT = ROOT / 'output/agentir-fusion-mmr-and-probe'
PILOT = ROOT / 'output/agentir-hundred-probe'


def fused_pool(arm, queries, rank_history, current_order, budget):
    return current_order, shared.fused_order(rank_history, queries)[:budget]


def fusion(pipe, worker):
    arm = 's3_current_mmr'
    controls = {
        'vanilla': shared.read_records(OLD_BASELINE, 'vanilla'),
        'rrf': shared.read_records(OLD_BASELINE, 's2_history_pool'),
        'mmr': shared.read_records(OLD_BASELINE / 'new_runs', arm),
    }
    baseline = controls['vanilla']
    ids = shared.read_json(OLD_BASELINE / 'vanilla/config.json')['sample_ids']
    assert len(ids) == len(set(ids)) == 200 and all(set(rows) == set(ids) for rows in controls.values())
    folder = OUT / 'fusion'
    rows = shared.read_records(folder, arm)
    positions = {name: i for i, name in enumerate(pipe.search_space)}
    shared.ROOT = INPUTS
    original = shared.rankings
    try:
        shared.rankings = fused_pool
        for i, qid in enumerate(ids, 1):
            if qid in rows:
                continue
            ref = baseline[qid]
            task = dict(qid=qid, img=ref['target'], caption=ref['queries'][0])
            worker.next_seed = 1021 + qid
            row = shared.run_dialogue(pipe, arm, task, positions[task['img']], hits_only=True)
            assert row['ranks'][:2] == controls['mmr'][qid]['ranks'][:2]
            rows[qid] = row
            shared.atomic_json(folder / arm / 'dialogues' / f'{qid}.json', row)
            print(f'Fusion {i}/200', flush=True)
    finally:
        shared.rankings = original
    curves = {name: [sum(min(data[i]['ranks'][:r+1]) <= 10 for i in ids) for r in range(11)]
              for name, data in dict(controls, fusion=rows).items()}
    good = sum(curves['fusion'][1:]) > sum(curves['vanilla'][1:]) and curves['fusion'][-1] >= curves['vanilla'][-1]
    shared.atomic_json(folder / 'summary.json', dict(n=len(ids), hit_counts=curves, tune_weights=good))
    print('Fusion hits by round:', curves, flush=True)
    if good:
        print('Reminder: compare MMR relevance/diversity weights against 0.7/0.3.', flush=True)


def support_weights(labels, n):
    counts = torch.ones(n)
    known = 0
    for label in labels:
        if label:
            counts[label-1] += 1
            known += 1
    if known < len(labels)/2:
        return None
    return counts / counts.sum()


def judge(worker, question, answers, captions):
    prompt = ('For each caption decide which single answer it explicitly supports for the question. '
              'Use 1-based answer numbers. Use 0 for missing, ambiguous, conflicting, or multiple answers. '
              'Return only a JSON array of integer labels, one per caption in the given order.\n'
              + json.dumps(dict(question=question, answers=answers, captions=captions)))
    raw = worker.complete([dict(role='user', content=prompt)], 100, temperature=0)
    try:
        labels = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
        if isinstance(labels, list) and len(labels) == len(captions) and all(
                type(v) is int and 0 <= v <= len(answers) for v in labels):
            return labels
    except ValueError:
        pass
    raise ValueError(f'Invalid caption labels: {raw!r}')


def js_score(probs, weights):
    def entropy(x):
        return -(x * x.clamp_min(1e-12).log()).sum(-1)
    return float(entropy(weights @ probs) - weights @ entropy(probs))


def weighted_case(case, pipe, worker, search, positions, img, extra):
    initial = case['initial']
    pool = search.similarity([initial], 'rank')[0].topk(1000).indices
    scores = {}
    details = {}
    for path in case['answer_paths']:
        q, answers = path['question'], path['answers']
        if q in scores or len(answers) < 2:
            continue
        labels = judge(worker, q, answers, case['evidence'])
        weights = support_weights(labels, len(answers))
        score = path['score']
        if weights is not None:
            sims = search.similarity([f'{initial}, {q} {a}' for a in answers], 'probe')
            probs = (sims[:, pool].cpu() / .05).softmax(dim=1)
            uniform = torch.ones(len(answers)) / len(answers)
            assert abs(js_score(probs, uniform) - score) < .001
            score = js_score(probs, weights)
        scores[q] = score
        details[q] = dict(labels=labels, weights=None if weights is None else weights.tolist(), score=score)
    ref = case['arms']['probe']
    chosen = max(scores, key=scores.get) if scores else ref['question']
    known = {a['question']: a for a in case['arms'].values()}
    known.update(extra)
    if chosen in known:
        outcome = known[chosen]
    else:
        answer = worker.answer(REPO / 'data' / img, chosen)
        rewrite = pipe.reconstruct_dialog([initial, f'{chosen} {answer}'])
        if rewrite == initial:
            rewrite = pipe.paraphrase(rewrite)
        order = search.similarity([rewrite], 'evaluation')[0].argsort().flip(0)
        outcome = dict(answer=answer, rewrite=rewrite, rank=int(shared.inverse_ranks(order)[positions[img]]))
    return dict(qid=case['qid'], chosen=chosen, plain=ref['question'], plain_rank=ref['rank'],
                outcome=outcome, evidence=details)


def probe(pipe, worker):
    folder = OUT / 'weighted_probe'
    ids = shared.read_json(PILOT / 'summary.json')['cases']
    assert len(set(ids)) == len(ids) == 100
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    extra = shared.read_json(ROOT / 'output/agentir-probe-pool-sweep-100/additional_outcomes.json')
    positions = {name: i for i, name in enumerate(pipe.search_space)}
    search = Search(pipe)
    rows = []
    for i, qid in enumerate(ids, 1):
        path = folder / 'cases' / f'{qid}.json'
        if not path.exists():
            case = shared.read_json(PILOT / 'cases' / f'{qid}.json')
            assert case['initial_rank'] > 10
            row = weighted_case(case, pipe, worker, search, positions, data[qid]['img'], extra.get(str(qid), {}))
            shared.atomic_json(path, row)
            print(f'Weighted probing {i}/100', flush=True)
        rows.append(shared.read_json(path))
    summary = dict(n=len(rows), changed=sum(r['chosen'] != r['plain'] for r in rows),
                   plain_hits=sum(r['plain_rank'] <= 10 for r in rows),
                   weighted_hits=sum(r['outcome']['rank'] <= 10 for r in rows))
    shared.atomic_json(folder / 'summary.json', summary)
    print(summary)


if __name__ == '__main__':
    if not torch.backends.mps.is_available():
        raise SystemExit('Start this runner from macOS Terminal with Metal access.')
    OUT.mkdir(exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock, torch.inference_mode():
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            fusion(pipe, worker)
            probe(pipe, worker)
        finally:
            worker.close()
