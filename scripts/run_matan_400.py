"""Matched proposal-count and visual-questioner comparisons for Matan."""
import argparse
from contextlib import ExitStack
import fcntl
from functools import partial
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch
from PIL import Image, ImageDraw, ImageFont, ImageOps

from run_fixed500_mmr85 import cohorts
from run_overnight_hits import INPUTS, OLD_BASELINE, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared, summarize

OUT = ROOT / 'output/matan-questioner-400'
GRID_WORKER = Path(__file__).with_name('matan_grid_worker.py')
ARMS = ('2.1a_vanilla_10', '2.1b_mmr85_10', '2.2b_vanilla_images_captions_5')


def question_candidates(pipe, dialogue, query, evidence, count):
    """Archived generation/filter/fallback loop, with a configurable proposal count."""
    questions, prior = [], []
    prompt = ''.join(f'{i}. {text}\n' for i, text in enumerate(evidence))
    for _ in range(count):
        for _ in range(3):
            result = pipe.generate_questions_referring(
                descrip=dialogue, prompt_fake=prompt, n=1, ques_prior=questions + prior)
            question = result['choices'][0]['message']['content'].split('?')[0] + '?'
            if 'uncertain' not in pipe.filter_questions(query, question):
                prior.append(question)
            else:
                break
        if len(prior) == 3:
            result = pipe.generate_questions(dialogue, n=1)
            question = result['choices'][0]['message']['content'].split('?')[0] + '?'
            if 'uncertain' not in pipe.filter_questions(query, question):
                question = 'what is the other object in the image?'
        questions.append(question)
    return questions


def build_grid(paths, output):
    if not 1 <= len(paths) <= 10:
        raise ValueError('Expected one to ten vanilla representatives')
    grid = Image.new('RGB', (1600, 640), (32, 32, 32))
    draw = ImageDraw.Draw(grid)
    font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 24)
    for i, path in enumerate(paths):
        x, y = (i % 5) * 320, (i // 5) * 320
        with Image.open(path) as image:
            tile = ImageOps.contain(ImageOps.exif_transpose(image).convert('RGB'), (320, 288))
        grid.paste(tile, (x + (320-tile.width)//2, y + 32 + (288-tile.height)//2))
        draw.text((x+8, y), str(i), font=font, fill='white')
    grid.save(output)


def install_visual_questioner(stack, pipe, grid):
    cluster = pipe.cluster_representatives
    create = pipe.client.create
    selected = []

    def select(pool):
        selected[:] = cluster(pool)
        build_grid([REPO/'data'/pipe.search_space[i] for i in selected], grid)
        return selected.copy()

    def visual_create(messages, **kwargs):
        captions = ''.join(f'{i}. {pipe.captions[idx]["caption"][0]}\n'
                           for i, idx in enumerate(selected))
        user = messages[-1].copy()
        content = user['content']
        if '[Retrieval Candidates]' in content:
            assert '[Retrieval Candidates]\n' + captions in content
        else:  # The baseline can fall back to its generic proposal prompt.
            content = '[Retrieval Candidates]\n' + captions + content
        user['content'] = ('Use the attached grid of candidate images together with the captions. '
                           'Each numbered tile corresponds to the caption with the same number.\n' + content)
        return create(messages=[*messages[:-1], user], image=grid, **kwargs)

    def wrap(generate):
        def call(*args, **kwargs):
            with patch.object(pipe.client, 'create', visual_create):
                return generate(*args, **kwargs)
        return call

    stack.enter_context(patch.object(pipe, 'cluster_representatives', select))
    for name in ('generate_questions_referring', 'generate_questions'):
        stack.enter_context(patch.object(pipe, name, wrap(getattr(pipe, name))))


def preflight():
    groups = cohorts()
    vanilla = {}
    mmr = {}
    for (name, tasks, control, inputs), folder in zip(groups, (OLD_BASELINE, OVERNIGHT_OUTPUT)):
        records = shared.read_records(folder, 'vanilla')
        for task in tasks:
            i = task['qid']
            row = records[i]
            assert (row['target'], row['queries'][0], row['ranks'][0]) == (
                task['img'], task['caption'], control[i]['ranks'][0])
            vanilla[i] = row
        mmr.update(control)
    for records, expected in [(vanilla, [290,320,338,349,360,362,363,367,368,372,376]),
                              (mmr, [290,328,345,360,365,366,370,371,372,373,376])]:
        assert [sum(min(r['ranks'][:k+1]) <= 10 for r in records.values()) for k in range(11)] == expected
    corpus = shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json')
    # List the directory once instead of issuing 50,000 individual stat calls.
    paths = {f'unlabeled2017/{p.name}' for p in (REPO/'data/unlabeled2017').iterdir()}
    assert len(corpus) == 50000 and set(corpus) <= paths
    print('Verified both plotted curves, 400 matched IDs and all 50,000 candidate image paths.', flush=True)
    return groups, vanilla, mmr


@torch.inference_mode()
def run(groups, vanilla, mmr):
    plan = dict(cohorts={name:tasks for name,tasks,_,_ in groups}, arms=list(ARMS),
                revision=SNAPSHOT.name, corpus=50000, stop_at_rank=10, rounds=10,
                pool='current query, shrinking 500 to 50', mmr_weight=0.85,
                grid=[1600,640], source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                for p in map(Path, [__file__, GRID_WORKER, shared.__file__, mac.__file__, mac.HERE/'generate_dialog.py'])})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Existing output belongs to a different experiment plan')
    shared.atomic_json(path, plan)
    if not torch.backends.mps.is_available():
        raise RuntimeError('Launch from Terminal on the Mac with Metal available.')
    with ExitStack() as session:
        # MacQwen starts the worker from its module entrypoint; reuse its client protocol.
        with patch.object(mac, '__file__', str(GRID_WORKER)):
            worker = mac.MacQwen(SNAPSHOT, OUT)
        session.callback(worker.close)
        pipe = mac.load_blip(INPUTS, worker)
        positions = {p:i for i,p in enumerate(pipe.search_space)}
        scratch = Path(session.enter_context(TemporaryDirectory()))
        for arm in ARMS:
            visual = arm == ARMS[2]
            use_mmr = arm == ARMS[1]
            count = 5 if visual else 10
            control = mmr if use_mmr else vanilla
            rows = shared.read_records(OUT, arm)
            assert rows.keys() <= control.keys()
            print(f'{arm}: starting at {len(rows)}/400', flush=True)
            with ExitStack() as settings:
                settings.enter_context(patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)))
                if visual:
                    install_visual_questioner(settings, pipe, scratch/'questioner-grid.png')
                else:
                    settings.enter_context(patch.object(shared, 'question_candidates', partial(question_candidates, count=count)))
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
                            worker.next_seed = 1021 + i
                            row = shared.run_dialogue(pipe, 's3_current_mmr' if use_mmr else 'vanilla',
                                                      task, positions[task['img']], hits_only=True)
                        assert row['ranks'][0] == ref['ranks'][0]
                        assert all(len(t['candidate_questions']) == count for t in row['turns'])
                        row['arm'] = arm
                        shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
                        rows[i] = row
                        print(f'{arm}: {len(rows)}/400 complete', flush=True)
            comparison = summarize(rows, control)
            for values in comparison.values():
                values['control_hits'] = values.pop('vanilla_hits')
            shared.atomic_json(OUT/arm/'summary.json', comparison)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    groups, vanilla, mmr = preflight()
    if not args.check_only:
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            torch.set_num_threads(4)
            run(groups, vanilla, mmr)
