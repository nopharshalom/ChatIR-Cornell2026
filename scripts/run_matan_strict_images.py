"""Run vanilla and MMR with one strict images-only question prompt after 2.2d."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

import torch

import run_matan_400 as base
import run_matan_images_only as images
import run_matan_mmr_visual as visual
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, shared

OUT = ROOT / 'output/matan-strict-images-400'
PREVIOUS = '2.2d_mmr85_images_captions_5'
ARMS = {
    '2.2e_vanilla_images_only_strict_5': ('vanilla', images.OUT, images.ARM),
    '2.2f_mmr85_images_only_strict_5': ('s3_current_mmr', visual.OUT, '2.2c_mmr85_images_only_5'),
}
STRICT = (
    "You are helping someone find their target photo. Only you see the grid. The user sees "
    "one target photo and can answer using only what is visible in it. Use differences among "
    "plausible candidates to pick one visible detail that would distinguish them. Ask about "
    "that detail directly, in a short standalone question that allows a specific answer. "
    "Avoid details already supplied or asked about. Never ask which image matches or refer "
    "to a grid, tile, number, candidate, or comparison between photos. Return only the question."
)


def install_strict(stack, pipe, grid, selector):
    create = pipe.client.create

    def strict_create(messages, **kwargs):
        if kwargs.get('image') == grid:
            # Keep the live description, answers and prior proposals. The archived
            # few-shot example requests an explanation and refers to numbered captions.
            content = messages[-1]['content'].split('[Description]\n', 1)[1]
            messages = [dict(role='system', content=STRICT),
                        dict(role='user', content='[Description]\n' + content.replace('[Dialogue]\n', '\n[Dialogue]\n', 1))]
        return create(messages=messages, **kwargs)

    stack.enter_context(patch.object(pipe.client, 'create', strict_create))
    if selector == 'vanilla':
        images.install_images_only(stack, pipe, grid)
    else:
        visual.install_arm(stack, pipe, grid, images.install_images_only)


def controls(groups):
    expected = {t['qid']: t for _, tasks, _, _ in groups for t in tasks}
    result = {}
    for name, (_, folder, arm) in ARMS.items():
        rows = shared.read_records(folder, arm)
        assert len(rows) == 400 and rows.keys() == expected.keys()
        for i, row in rows.items():
            assert (row['target'], row['queries'][0]) == (expected[i]['img'], expected[i]['caption'])
        result[name] = rows
    return result


def wait_for_previous(ids):
    path = visual.OUT/PREVIOUS/'summary.json'
    if not path.is_file():
        print('Queued. Waiting for the completed 2.2d summary before opening the GPU.', flush=True)
    while not path.is_file():
        time.sleep(15)
    with (visual.OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        summary = shared.read_json(path)
        previous = shared.read_records(visual.OUT, PREVIOUS)
        if previous.keys() != ids or summary['10']['total'] != 400:
            raise RuntimeError('2.2d completion records do not match the expected 400 dialogues.')


@torch.inference_mode()
def run(groups, references):
    sources = [__file__, images.__file__, visual.__file__, base.__file__, shared.__file__,
               mac.__file__, GRID_WORKER, mac.HERE/'generate_dialog.py']
    plan = dict(cohorts={n:t for n,t,_,_ in groups}, arms=list(ARMS), prompt=STRICT, corpus=50000,
                grid=[1600,640], proposals=5, rounds=10, stop_at_rank=10, mmr_weight=.85,
                pool='current query, shrinking 500 to 50', revision=SNAPSHOT.name,
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in map(Path,sources)})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise RuntimeError('Saved plan differs from the requested run.')
    shared.atomic_json(path, plan)
    if not torch.backends.mps.is_available():
        raise RuntimeError('Run the launcher in Terminal with Metal available.')
    with ExitStack() as session:
        with patch.object(mac, '__file__', str(GRID_WORKER)):
            worker = mac.MacQwen(SNAPSHOT, OUT)
        session.callback(worker.close)
        pipe = mac.load_blip(INPUTS, worker)
        positions = {p:i for i,p in enumerate(pipe.search_space)}
        grid = Path(session.enter_context(TemporaryDirectory()))/'questioner-grid.png'
        for arm, (selector, _, _) in ARMS.items():
            control = references[arm]
            rows = shared.read_records(OUT, arm)
            assert rows.keys() <= control.keys()
            print(f'{arm}: starting at {len(rows)}/400', flush=True)
            with ExitStack() as stack:
                install_strict(stack, pipe, grid, selector)
                for _, tasks, _, inputs in groups:
                    shared.ROOT = inputs
                    for task in tasks:
                        i = task['qid']
                        if i in rows:
                            continue
                        ref = control[i]
                        if ref['ranks'][0] <= 10:
                            assert not ref['turns']
                            row = dict(ref)
                        else:
                            worker.next_seed = 1021 + i
                            row = shared.run_dialogue(pipe, selector, task, positions[task['img']], hits_only=True)
                            assert row['turns'][0]['selected_ids'] == ref['turns'][0]['selected_ids']
                        assert row['ranks'][0] == ref['ranks'][0]
                        assert all(len(t['candidate_questions']) == 5 for t in row['turns'])
                        row['arm'] = arm
                        shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
                        rows[i] = row
                        print(f'{arm}: {len(rows)}/400 complete', flush=True)
            report = base.summarize(rows, control)
            for entry in report.values():
                entry['original_prompt_hits'] = entry.pop('vanilla_hits')
            shared.atomic_json(OUT/arm/'summary.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    groups, _, _ = base.preflight()
    refs = controls(groups)
    print('Verified both images-only controls against the original 400 IDs.', flush=True)
    if not args.check_only:
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            wait_for_previous(next(iter(refs.values())).keys())
            torch.set_num_threads(4)
            run(groups, refs)
