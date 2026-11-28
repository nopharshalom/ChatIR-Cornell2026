"""Matched ten-dialogue Qwen pilot for the repaired images-only questioner."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch

import run_matan_400 as base
from run_matan_mmr_visual import render_mmr_evidence

IDS = (532, 2019, 722, 526, 494, 360, 1647, 1109, 896, 1515)
OUT = base.ROOT / 'output/matan-repaired-images-pilot-10'
ARM = 'mmr85_images_only_repaired'
SYSTEM = """Generate one question for a person who sees only their target photo.
You see ten candidate photos. Find an object shared by the description and plausible candidates.
Ask for a visible attribute of that object whose value differs across candidates and is still unknown.
Prefer asking what the value is (color, clothing, number, action, location) over guessing it.
Use the grid privately. The question must make sense to someone looking only at their own photo.
Example: the description says a skateboarder is doing a trick. The dialogue already establishes
a jump at a skate park. Candidate skateboarders wear different outfits.
A useful next question is: What is the skateboarder wearing?
Return one complete standalone question ending in a question mark."""
START = re.compile(r"^(what|which|where|who|whose|when|why|how|is|are|was|were|does|do|did|can|could|would|has|have)\b", re.I)


def parse_question(raw):
    # Never convert an answer into a question by appending punctuation.
    text = re.sub(r'^question:\s*', '', raw.strip().strip('*" '), flags=re.I).strip('*" ')
    if (not START.match(text) or not text.endswith('?') or text.count('?') != 1
            or '\n' in text or len(re.findall(r"\b[\w'-]+\b", text)) < 3):
        return None
    return text


def install(stack, pipe, grid, rejected):
    def render(ids):
        base.build_grid([base.REPO/'data'/pipe.search_space[i] for i in ids], grid)
        return ids

    def propose(descrip, prompt_fake='', n=1, ques_prior=(), **kwargs):
        completed = '\n'.join(descrip[1:]) or '(none)'
        prior = '\n'.join('- ' + q for q in ques_prior) or '(none)'
        user = (f'Target description:\n{descrip[0]}\nCompleted dialogue:\n{completed}\n'
                f'Questions already proposed this round:\n{prior}\nWrite one new question.')
        for attempt in range(6):
            messages = [dict(role='system', content=SYSTEM), dict(role='user', content=user)]
            response = pipe.client.create(messages=messages, image=grid, max_tokens=32, temperature=.5)
            message = response['choices'][0]['message']
            question = parse_question(message['content'])
            if question:
                message['content'] = question
                return response
            rejected.append(message['content'])
            if attempt == 0:
                user += '\nWrite the complete question, including its subject and a final question mark.'
        raise RuntimeError('Six malformed proposals in a row. Last output: ' + repr(rejected[-1]))

    stack.enter_context(patch.object(pipe, 'cluster_representatives', render))
    stack.enter_context(patch.object(base.shared, 'representatives', render_mmr_evidence))
    # The existing filter and minimum-KL selector receive only validated questions.
    for name in ('generate_questions_referring', 'generate_questions'):
        stack.enter_context(patch.object(pipe, name, propose))


def sample():
    original = base.shared.read_records(base.ROOT/'output/matan-mmr-visual-400', '2.2c_mmr85_images_only_5')
    strict = base.shared.read_records(base.ROOT/'output/matan-strict-images-400', '2.2f_mmr85_images_only_strict_5')
    tasks = {t['qid']: (t, inputs) for _, group, _, inputs in base.cohorts() for t in group}
    for i in IDS:
        t, _ = tasks[i]
        assert (original[i]['target'], original[i]['queries'][0]) == (t['img'], t['caption'])
        assert original[i]['ranks'][0] == strict[i]['ranks'][0] > 10
    return tasks, original, strict


def hits(rows):
    return [sum(min(rows[i]['ranks'][:k+1]) <= 10 for i in IDS) for k in range(11)]


@torch.inference_mode()
def main(check_only):
    tasks, original, strict = sample()
    print('Verified 10 original pilot IDs. Round 0–10 control hits:', flush=True)
    print('Original', hits(original), '\nStrict  ', hits(strict), flush=True)
    if check_only:
        return
    if not torch.backends.mps.is_available():
        raise SystemExit('Metal is unavailable in this session. Launch the pilot from macOS Terminal.')
    OUT.mkdir(exist_ok=True)
    with ExitStack() as stack:
        lock = stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = dict(ids=IDS, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    prompt=SYSTEM, model=base.SNAPSHOT.name, proposals=5, rounds=10, mmr=.85)
        plan['ids'] = list(IDS)
        path = OUT/'plan.json'
        if path.exists() and base.shared.read_json(path) != plan:
            raise ValueError('Pilot configuration differs from existing results.')
        base.shared.atomic_json(path, plan)
        rows = base.shared.read_records(OUT, ARM)
        assert rows.keys() <= set(IDS)
        if len(rows) < len(IDS):
            with patch.object(base.mac, '__file__', str(base.GRID_WORKER)):
                worker = base.mac.MacQwen(base.SNAPSHOT, OUT)
            stack.callback(worker.close)
            pipe = base.mac.load_blip(base.INPUTS, worker)
            positions = {p:i for i,p in enumerate(pipe.search_space)}
            grid = Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
            rejected = []
            install(stack, pipe, grid, rejected)
            for i in IDS:
                if i in rows:
                    continue
                task, base.shared.ROOT = tasks[i]
                rejected.clear()
                worker.next_seed = 1021 + i
                row = base.shared.run_dialogue(pipe, 's3_current_mmr', task, positions[task['img']], hits_only=True)
                assert row['ranks'][0] == original[i]['ranks'][0]
                assert row['turns'][0]['selected_ids'] == original[i]['turns'][0]['selected_ids']
                assert all(len(t['candidate_questions']) == 5 and all(parse_question(q) for q in t['candidate_questions']) for t in row['turns'])
                row.update(arm=ARM, rejected_outputs=rejected.copy())
                base.shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json',row)
                rows[i] = row
                print(f"{len(rows)}/10, ID {i}, ranks {row['ranks']}, first question {row['turns'][0]['question']}", flush=True)
        summary = {name:hits(records) for name,records in [('original',original),('strict',strict),('repaired',rows)]}
        base.shared.atomic_json(OUT/'summary.json',summary)
        print(summary,flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    main(parser.parse_args().check_only)
