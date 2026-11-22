"""Evaluate the alternative first questions on the rejected-caption pilot's paired losses."""
import fcntl

import torch

from run_rejected_caption_pilot import (
    INPUTS, OUT as PILOT, REPO, ROOT, SNAPSHOT, Search, mac, score_outcome, shared,
)

OUT = ROOT / 'output/agentir-rejected-caption-lost-candidates'
METRICS = ('raw_rank', 'unseen_rank')


def report(cases, results):
    summary = {}
    for metric in METRICS:
        rescued, missed, pending = [], [], []
        for case in cases:
            if not case['control'][metric] <= 10 < case['variant'][metric]:
                continue
            qid = str(case['qid'])
            candidates = set(case['candidates']) - {case['variant']['question']}
            outcomes = results.get(qid, {})
            if any(outcomes[q][metric] <= 10 for q in candidates if q in outcomes):
                rescued.append(qid)
            elif candidates <= outcomes.keys():
                missed.append(qid)
            else:
                pending.append(qid)
        summary[metric] = dict(rescued=rescued, none_hit=missed, pending=pending)
        print(f'{metric}: {len(rescued)} rescuable, {len(missed)} with no hit, {len(pending)} unresolved.')
    shared.atomic_json(OUT / 'summary.json', summary)


@torch.inference_mode()
def run():
    plan = shared.read_json(PILOT / 'plan.json')
    cases = [shared.read_json(PILOT / 'cases' / f'{i}.json') for i in plan['qids']]
    assert len(cases) == 100 and len({c['qid'] for c in cases}) == 100
    cases = [c for c in cases if any(c['control'][m] <= 10 < c['variant'][m] for m in METRICS)]
    sweep = shared.read_json(ROOT / 'output/agentir-probe-pool-sweep-100/additional_outcomes.json')
    outcome_path = OUT / 'outcomes.json'
    results = shared.read_json(outcome_path) if outcome_path.exists() else {}
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    positions = {name: i for i, name in enumerate(shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json'))}
    pending = []
    for case in cases:
        qid = str(case['qid'])
        saved = shared.read_json(ROOT / 'output/agentir-hundred-probe/cases' / f'{qid}.json')
        known = {arm['question']: arm for arm in saved['arms'].values()}
        known.update(sweep.get(qid, {}))
        assert saved['initial'] == case['initial'] and case['variant']['question'] in case['candidates']
        for q in dict.fromkeys(case['candidates']):
            if q != case['variant']['question'] and q not in results.get(qid, {}):
                pending.append((case, q, known.get(q)))
    if not pending:
        report(cases, results)
        return
    # Recover full metrics for previously answered alternatives without any generation.
    from run_probe_pool_sweep import cpu_retriever
    from run_rejected_caption_pilot import metrics
    cached = [(c, q, a) for c, q, a in pending if a]
    if cached:
        torch.set_num_threads(4)
        score = cpu_retriever()
        for c, q, a in cached:
            target = positions[data[c['qid']]['img']]
            order = score([a['rewrite']])[0].argsort().flip(0)
            ranks = metrics(order, target, torch.tensor(c['displayed_ids']))
            assert ranks['raw_rank'] == a['rank']
            results.setdefault(str(c['qid']), {})[q] = {**a, **ranks}
        shared.atomic_json(outcome_path, results)
        del score
    pending = [(c, q) for c, q, a in pending if not a]
    report(cases, results)
    if not pending:
        return
    print(f'{len(pending)} new target-image answers needed.', flush=True)
    if not torch.backends.mps.is_available():
        raise SystemExit('Continue with this script in macOS Terminal for Metal access.')
    worker = mac.MacQwen(SNAPSHOT, OUT)
    try:
        pipe = mac.load_blip(INPUTS, worker)
        search = Search(pipe)
        for i, (c, q) in enumerate(pending, 1):
            img = data[c['qid']]['img']
            answer = worker.answer(REPO / 'data' / img, q)
            rewrite = pipe.reconstruct_dialog([c['initial'], f'{q} {answer}'])
            if rewrite == c['initial']:
                rewrite = pipe.paraphrase(rewrite)
            outcome = score_outcome(search, dict(question=q, answer=answer, rewrite=rewrite),
                                    positions[img], torch.tensor(c['displayed_ids'], device=pipe.img_embs.device))
            results.setdefault(str(c['qid']), {})[q] = outcome
            shared.atomic_json(outcome_path, results)
            print(f"{i}/{len(pending)} · QID {c['qid']} · rank {outcome['raw_rank']}", flush=True)
    finally:
        worker.close()
    report(cases, results)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run()
