"""Experiment 2.2a: vanilla clustering, image grid, five proposals."""
import argparse
from contextlib import ExitStack
import fcntl
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch
import run_matan_400 as base
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, build_grid, mac, shared

ARM = '2.2a_vanilla_images_only_5'
OUT = ROOT / 'output/matan-images-only-400'


def install_images_only(stack, pipe, grid):
    cluster = pipe.cluster_representatives
    create = pipe.client.create

    def select(pool):
        ids = cluster(pool)
        build_grid([REPO/'data'/pipe.search_space[i] for i in ids], grid)
        return ids

    def visual_create(messages, **kwargs):
        messages = [m.copy() for m in messages]
        messages[0]['content'] = messages[0]['content'].replace(
            'These are captions of images which are the candidates', 'These are images which are the candidates')
        user = messages[-1]
        user['content'] = ('Use the attached numbered grid of candidate images as retrieval evidence.\n'
                           + user['content'])
        return create(messages=messages, image=grid, **kwargs)

    def wrap(generate):
        def call(*args, **kwargs):
            if 'prompt_fake' in kwargs:
                kwargs['prompt_fake'] = ''  # Retain the description, dialogue and proposal history.
            with patch.object(pipe.client, 'create', visual_create):
                return generate(*args, **kwargs)
        return call

    stack.enter_context(patch.object(pipe, 'cluster_representatives', select))
    for name in ('generate_questions_referring', 'generate_questions'):
        stack.enter_context(patch.object(pipe, name, wrap(getattr(pipe, name))))


@torch.inference_mode()
def run(groups, control):
    plan = dict(arm=ARM, cohorts={name:tasks for name,tasks,_,_ in groups}, corpus=50000,
                revision=SNAPSHOT.name, grid=[1600,640], proposals=5, max_rounds=10, stop_at_rank=10,
                sources={p.name:base.hashlib.sha256(p.read_bytes()).hexdigest() for p in
                         map(Path,[__file__,base.__file__,GRID_WORKER,mac.__file__,shared.__file__,mac.HERE/'generate_dialog.py'])})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Existing checkpoints use a different run configuration')
    shared.atomic_json(path, plan)
    rows = shared.read_records(OUT, ARM)
    assert rows.keys() <= control.keys()
    if len(rows) < 400:
        with ExitStack() as stack:
            if not torch.backends.mps.is_available():
                raise RuntimeError('Launch from Terminal with Metal available.')
            with patch.object(mac, '__file__', str(GRID_WORKER)):
                worker = mac.MacQwen(SNAPSHOT, OUT)
            stack.callback(worker.close)
            pipe = mac.load_blip(INPUTS, worker)
            positions = {image:i for i,image in enumerate(pipe.search_space)}
            grid = Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
            install_images_only(stack, pipe, grid)
            for _, tasks, _, inputs in groups:
                shared.ROOT = inputs
                for task in tasks:
                    i = task['qid']
                    if i in rows:
                        continue
                    ref = control[i]
                    if ref['ranks'][0] <= 10:
                        row = dict(ref)
                    else:
                        worker.next_seed = 1021+i
                        row = shared.run_dialogue(pipe,'vanilla',task,positions[task['img']],hits_only=True)
                    assert row['ranks'][0] == ref['ranks'][0]
                    assert all(len(t['candidate_questions']) == 5 for t in row['turns'])
                    row['arm'] = ARM
                    shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json',row)
                    rows[i] = row
                    print(f'{ARM}: {len(rows)}/400 complete',flush=True)
    shared.atomic_json(OUT/ARM/'summary.json',base.summarize(rows, control))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    args = parser.parse_args()
    groups, control, _ = base.preflight()
    if not args.check_only:
        OUT.mkdir(parents=True,exist_ok=True)
        with (OUT/'run.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            torch.set_num_threads(4)
            run(groups,control)
