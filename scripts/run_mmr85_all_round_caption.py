"""One matched MMR 0.85 run with current-query caption probing on every asked turn."""
import fcntl
import hashlib
from functools import partial
from pathlib import Path
from unittest.mock import patch

import run_vanilla_all_round_probes as probes
from run_mmr_history_visual_200 import OUT as MMR_OUTPUT, BASE
from run_overnight_hits import INPUTS, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared, summarize
import torch

ARM = 'mmr85_caption_all_rounds'
OUT = ROOT / 'output/agentir-mmr85-all-round-caption-200'


def run_caption(pipe, task, target, reserved):
    original = shared.run_dialogue
    def use_mmr(model, selector, *args, **kwargs):
        assert selector == 'vanilla'
        return original(model, BASE, *args, **kwargs)

    with patch.object(shared, 'run_dialogue', use_mmr), patch.object(
            shared, 'history_mmr', partial(shared.history_mmr, weight=.85)):
        row = probes.probe_dialogue(pipe, task, target, 'caption_all_rounds', reserved)
    row['arm'] = ARM
    return row


@torch.inference_mode()
def main():
    tasks = shared.read_json(MMR_OUTPUT/'plan.json')['tasks']
    controls = shared.read_records(MMR_OUTPUT, 'mmr85')
    assert len(tasks) == len(controls) == 200 and set(controls) == {t['qid'] for t in tasks}
    plan = dict(tasks=tasks, arm=ARM, mmr=.85, rounds=10, corpus=50000, snapshot=SNAPSHOT.name,
                source_hashes=[hashlib.sha256(Path(f).read_bytes()).hexdigest()
                               for f in (__file__, probes.__file__, shared.__file__)])
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Run configuration changed since the last checkpoint.')
    shared.atomic_json(path, plan)
    rows = shared.read_records(OUT, ARM)
    assert rows.keys() <= controls.keys()
    def save(row):
        i = row['qid']
        shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json', row)
        rows[i] = row
    for task in tasks:
        i = task['qid']
        ref = controls[i]
        assert (ref['target'], ref['queries'][0]) == (task['img'], task['caption'])
        if i not in rows and not ref['turns']:
            save(dict(ref, arm=ARM, probes=[]))
    print(f'{ARM}: {len(rows)}/200 completed', flush=True)
    if len(rows) < 200:
        if not torch.backends.mps.is_available():
            raise SystemExit('Remaining dialogues need Metal. Run Run_MMR85_All_Round_Caption.command on the Mac.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            shared.ROOT = OVERNIGHT_OUTPUT/'fresh-inputs'
            positions = {name:i for i,name in enumerate(pipe.search_space)}
            reserved = {positions[t['img']] for t in shared.read_json(REPO/'dialogues/VisDial_v1.0_queries_val.json')
                        if t['img'] in positions}
            for task in tasks:
                i = task['qid']
                if i in rows:
                    continue
                row = run_caption(pipe, task, positions[task['img']], reserved)
                ref = controls[i]
                assert row['ranks'][0] == ref['ranks'][0]
                assert row['turns'][0]['selected_ids'] == ref['turns'][0]['selected_ids']
                assert row['turns'][0]['candidate_questions'] == ref['turns'][0]['candidate_questions']
                assert len(row['probes']) == len(row['turns'])
                save(row)
                print(f'{ARM}: {len(rows)}/200 completed', flush=True)
        finally:
            worker.close()
    result = summarize(rows, controls)
    shared.atomic_json(OUT/ARM/'vs_plain_mmr85.json', result)
    print('Round 10 vs plain MMR 0.85:', result[10])


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        torch.set_num_threads(4)
        main()
