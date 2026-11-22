"""Two comparisons on the paper's archived original 200, in manifest order."""
import argparse
import fcntl
import hashlib
from functools import partial
from pathlib import Path
from unittest.mock import patch

import torch

import run_vanilla_all_round_probes as probes
from run_overnight_hits import INPUTS, REPO, ROOT, SNAPSHOT, mac, shared, summarize

ARCHIVE = ROOT/'output/plugir-overnight-hits10-200'
OUT = ROOT/'output/paper-original200-mmr85-and-caption'
ARMS = ('current_mmr85', 'caption_all_rounds')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight():
    tasks = shared.read_json(ARCHIVE/'sample.json')['tasks']
    ids = [t['qid'] for t in tasks]
    assert len(set(ids)) == len({t['img'] for t in tasks}) == len(tasks) == 200
    controls = {}
    for name, folder, arm, counts in (
            ('vanilla', ARCHIVE/'results', 'vanilla', (177,187)),
            ('current_mmr70', ARCHIVE/'results/new_runs', 's3_current_mmr', (177,189))):
        cfg = shared.read_json((folder/'vanilla' if name == 'vanilla' else folder)/'config.json')
        assert cfg['sample_ids'] == ids and cfg['revision'] == SNAPSHOT.name
        assert cfg['corpus_size'] == 50000 and cfg['stop_at_rank'] == cfg['max_rounds'] == 10
        if name == 'current_mmr70':
            assert cfg['mmr_weight'] == .7
            for filename, expected in cfg['source_sha256'].items():
                assert digest(mac.HERE/filename) == expected
        rows = shared.read_records(folder, arm)
        assert set(rows) == set(ids)
        for t in tasks:
            row = rows[t['qid']]
            assert (row['target'], row['queries'][0]) == (t['img'],t['caption'])
            assert (INPUTS/'targets'/t['img']).is_file()
        assert tuple(sum(min(r['ranks'][:k+1]) <= 10 for r in rows.values()) for k in (5,10)) == counts
        controls[name] = rows
    print('Verified paper original 200. R10 controls: vanilla 187/200, current MMR 0.70 189/200.', flush=True)
    return tasks, controls


@torch.inference_mode()
def run(tasks, controls):
    plan = dict(tasks=tasks, sample_sha256=digest(ARCHIVE/'sample.json'), corpus=50000,
                arms=list(ARMS), mmr_weight=.85, probe_selector='vanilla clustering',
                caption_probe_questions=list(range(1,11)), stop_at_rank=10, revision=SNAPSHOT.name,
                code=[digest(Path(p)) for p in (__file__, probes.__file__, shared.__file__)])
    plan_file = OUT/'plan.json'
    if plan_file.exists() and shared.read_json(plan_file) != plan:
        raise ValueError('Checkpoint configuration differs from the original-200 plan.')
    shared.atomic_json(plan_file, plan)
    worker = None
    shared.ROOT = INPUTS
    try:
        for arm in ARMS:
            rows = shared.read_records(OUT, arm)
            assert rows.keys() <= controls['vanilla'].keys()
            for task in tasks:
                i = task['qid']
                if i in rows:
                    continue
                ref = controls['vanilla'][i]
                if not ref['turns']:
                    row = dict(ref)
                    if arm == 'caption_all_rounds':
                        row['probes'] = []
                else:
                    if worker is None:
                        if not torch.backends.mps.is_available():
                            raise SystemExit('GPU unavailable. Launch this command in Terminal on your Mac.')
                        worker = mac.MacQwen(SNAPSHOT, OUT)
                        pipe = mac.load_blip(INPUTS, worker)
                        positions = {name:j for j,name in enumerate(pipe.search_space)}
                        reserved = {positions[t['img']] for t in shared.read_json(REPO/'dialogues/VisDial_v1.0_queries_val.json') if t['img'] in positions}
                    if arm == 'current_mmr85':
                        worker.next_seed = 1021 + i
                        with patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)):
                            row = shared.run_dialogue(pipe, 's3_current_mmr', task, positions[task['img']], hits_only=True)
                    else:
                        row = probes.probe_dialogue(pipe, task, positions[task['img']], arm, reserved)
                        assert [p['round'] for p in row['probes']] == list(range(1,len(row['turns'])+1))
                assert row['qid'] == i and row['ranks'][0] == ref['ranks'][0]
                row['arm'] = arm
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
                rows[i] = row
                print(f'{arm}: {len(rows)}/200',flush=True)
            shared.atomic_json(OUT/arm/'summary.json', {k:summarize(rows,v) for k,v in controls.items()})
    finally:
        if worker is not None:
            worker.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    args = parser.parse_args()
    tasks, controls = preflight()
    if not args.check_only:
        OUT.mkdir(exist_ok=True,parents=True)
        with (OUT/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            torch.set_num_threads(4)
            run(tasks,controls)
