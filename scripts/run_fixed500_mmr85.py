"""Paired fixed-width vs shrinking-pool MMR 0.85 on the saved 400 dialogues."""
import argparse
import fcntl
from functools import partial
from unittest.mock import patch

import torch

from run_overnight_hits import INPUTS, ROOT, SNAPSHOT, mac, shared

OUTPUT = ROOT / 'output'
OUT = OUTPUT / 'mmr85-current-top500-400'
ARM = 'current_mmr85_fixed500'


def cohorts():
    paper = OUTPUT / 'paper-original200-mmr85-and-caption'
    fresh = OUTPUT / 'agentir-mmr-history-visual-200'
    groups = []
    for name, manifest, folder, arm, inputs in [
        ('paper', OUTPUT/'plugir-overnight-hits10-200/sample.json', paper,
         'current_mmr85', INPUTS),
        ('additional', OUTPUT/'agentir-vanilla-all-round-probes-200/plan.json', fresh,
         'mmr85', OUTPUT/'agentir-overnight-200-300/fresh-inputs'),
    ]:
        tasks = shared.read_json(manifest)['tasks']
        controls = shared.read_records(folder, arm)
        assert len(tasks) == 200 and {t['qid'] for t in tasks} == controls.keys()
        for t in tasks:
            ref = controls[t['qid']]
            assert (ref['qid'], ref['target'], ref['queries'][0]) == (t['qid'], t['img'], t['caption'])
            assert (inputs/'targets'/t['img']).is_file()
            assert len(ref['ranks']) == 11 or min(ref['ranks']) <= 10
        groups.append((name, tasks, controls, inputs))
    for key in ['qid', 'img']:
        assert len({t[key] for _, tasks, _, _ in groups for t in tasks}) == 400
    return groups


def fixed_pool(arm, queries, rank_history, current_order, budget):
    return current_order, current_order[:500]


@torch.inference_mode()
def run(groups, out, arm, history):
    pool_rule = 'all-query RRF shrinking 500 to 50' if history else 'current query top 500 every question'
    plan = dict(arm=arm, mmr_weight=.85, pool=pool_rule,
                revision=SNAPSHOT.name, corpus=50000, max_questions=10,
                cohorts={name: tasks for name, tasks, _, _ in groups})
    path = out/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Saved plan differs')
    shared.atomic_json(path, plan)
    if (out/'summary.json').exists() and len(shared.read_records(out, arm)) == 400:
        print(f'{arm} already complete', flush=True)
        return
    worker = mac.MacQwen(SNAPSHOT, out)
    try:
        pipe = mac.load_blip(INPUTS, worker)
        positions = {img: j for j, img in enumerate(pipe.search_space)}
        assert len(positions) == 50000
        rows = shared.read_records(out, arm)
        curves = []
        pool_fn = partial(shared.rankings, 's2_history_pool')
        def history_pool(_arm, queries, rank_history, current_order, budget):
            return pool_fn(queries, rank_history, current_order, budget)

        with patch.object(shared, 'rankings', history_pool if history else fixed_pool), patch.object(
                shared, 'history_mmr', partial(shared.history_mmr, weight=.85)):
            for name, tasks, controls, inputs in groups:
                shared.ROOT = inputs
                for task in tasks:
                    qid = task['qid']
                    ref = controls[qid]
                    if qid not in rows:
                        # Through Q1 both schedules use exactly the same evidence pool.
                        if min(ref['ranks'][:2]) <= 10:
                            row = dict(ref)
                        else:
                            worker.next_seed = 1021 + qid
                            row = shared.run_dialogue(pipe, 's3_current_mmr', task,
                                                      positions[task['img']], hits_only=True)
                        assert row['ranks'][:2] == ref['ranks'][:2]
                        assert row['queries'][:2] == ref['queries'][:2]
                        assert all(len(t['pool_ids']) == (500 - 50*t['round'] if history else 500) for t in row['turns'])
                        row['arm'] = arm
                        shared.atomic_json(out/arm/'dialogues'/f'{qid}.json', row)
                        rows[qid] = row
                        print(f'{len(rows)}/400 completed', flush=True)
                for k in range(11):
                    hits = {i for i in controls if min(rows[i]['ranks'][:k+1]) <= 10}
                    base = {i for i in controls if min(controls[i]['ranks'][:k+1]) <= 10}
                    curves.append(dict(cohort=name, round=k, n=len(controls), **{('s2_mmr85' if history else 'fixed500'): len(hits)},
                                       shrinking=len(base), gained=len(hits-base), lost=len(base-hits)))
        shared.atomic_json(out/'summary.json', curves)
    finally:
        worker.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--s2-history', action='store_true')
    parser.add_argument('--wait', action='store_true', help='Wait for an active copy of this experiment')
    args = parser.parse_args()
    out = OUTPUT/'mmr85-s2-history-400' if args.s2_history else OUT
    arm = 'current_mmr85_s2_history' if args.s2_history else ARM
    groups = cohorts()
    print('Verified both 200-dialogue manifests, MMR controls and target files.', flush=True)
    if not args.check_only:
        if not torch.backends.mps.is_available():
            raise SystemExit('Metal unavailable. Launch in Terminal on the Mac.')
        out.mkdir(parents=True, exist_ok=True)
        with (out/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if args.wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            torch.set_num_threads(4)
            run(groups, out, arm, args.s2_history)
