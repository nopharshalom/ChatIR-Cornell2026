"""Queue 2.2c and 2.2d on the original two 200-dialogue cohorts."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch

import run_matan_400 as base
import run_matan_images_only as images
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, shared

OUT = ROOT / 'output/matan-mmr-visual-400'
ARMS = {
    '2.2c_mmr85_images_only_5': images.install_images_only,
    '2.2d_mmr85_images_captions_5': base.install_visual_questioner,
}


def render_mmr_evidence(arm, pool, history_rank, embeddings, render):
    assert arm == 's3_current_mmr'
    selected = shared.history_mmr(pool, history_rank, embeddings, count=10, weight=.85)
    assert len(set(selected)) == 10
    return render(selected)


def install_arm(stack, pipe, grid, installer):
    # The vanilla grid builders call a selector before drawing. Supply the ten
    # already selected MMR IDs there so both prompts use the original grid code.
    stack.enter_context(patch.object(pipe, 'cluster_representatives', list))
    installer(stack, pipe, grid)
    stack.enter_context(patch.object(shared, 'representatives', render_mmr_evidence))


def run_arm(arm, groups, control, pipe, worker, positions):
    rows = shared.read_records(OUT, arm)
    assert rows.keys() <= control.keys()
    print(f'{arm}: starting at {len(rows)}/400', flush=True)
    for _, tasks, _, inputs in groups:
        shared.ROOT = inputs
        for task in tasks:
            qid = task['qid']
            if qid in rows:
                continue
            ref = control[qid]
            if ref['ranks'][0] <= 10:
                assert not ref['turns']
                row = dict(ref)
            else:
                worker.next_seed = 1021 + qid
                row = shared.run_dialogue(pipe, 's3_current_mmr', task,
                                          positions[task['img']], hits_only=True)
                assert row['turns'][0]['selected_ids'] == ref['turns'][0]['selected_ids']
            assert row['ranks'][0] == ref['ranks'][0]
            assert all(len(t['candidate_questions']) == 5 for t in row['turns'])
            row['arm'] = arm
            shared.atomic_json(OUT/arm/'dialogues'/f'{qid}.json', row)
            rows[qid] = row
            print(f'{arm}: {len(rows)}/400 complete', flush=True)
    comparison = base.summarize(rows, control)
    for values in comparison.values():
        values['mmr_caption_hits'] = values.pop('vanilla_hits')
    shared.atomic_json(OUT/arm/'summary.json', comparison)


@torch.inference_mode()
def run(groups, control):
    sources = [__file__, base.__file__, images.__file__, shared.__file__, mac.__file__,
               GRID_WORKER, mac.HERE/'generate_dialog.py']
    plan = dict(cohorts={name: tasks for name, tasks, _, _ in groups}, arms=list(ARMS),
                mmr_weight=.85, proposals=5, pool='current query, shrinking 500 to 50',
                grid=[1600, 640], corpus=50000, rounds=10, stop_at_rank=10, revision=SNAPSHOT.name,
                sources={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in map(Path, sources)})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Saved experiment configuration differs')
    shared.atomic_json(path, plan)
    if not torch.backends.mps.is_available():
        raise RuntimeError('Launch from Terminal with Metal available.')
    with ExitStack() as session:
        with patch.object(mac, '__file__', str(GRID_WORKER)):
            worker = mac.MacQwen(SNAPSHOT, OUT)
        session.callback(worker.close)
        pipe = mac.load_blip(INPUTS, worker)
        positions = {p: i for i, p in enumerate(pipe.search_space)}
        grid = Path(session.enter_context(TemporaryDirectory()))/'questioner-grid.png'
        for arm, installer in ARMS.items():
            with ExitStack() as settings:
                install_arm(settings, pipe, grid, installer)
                run_arm(arm, groups, control, pipe, worker, positions)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    groups, _, control = base.preflight()
    if not args.check_only:
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            torch.set_num_threads(4)
            run(groups, control)
