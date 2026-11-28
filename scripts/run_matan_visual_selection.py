"""Compare minimum KL with a ten-image answer-split selector on matched MMR dialogues."""
import argparse
from collections import Counter
from contextlib import ExitStack
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import random
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch
import run_matan_400 as base

ARM = 'mmr85_visual_question_selection'
OUT = base.ROOT / 'output/matan-visual-question-selection-20'
PROMPT = """You see ten numbered candidate images and a list of questions. For EACH question,
estimate the answer separately for EACH image, in tile order 0 through 9. Each tile is a
possible searcher's own photo. Use the current description only to resolve references.
Return one JSON array of rows, in question order. Each row is an array of ten integers.
Within a row, the same answer means the same positive integer (start with 1). Merge
synonyms, paraphrases and minor shades. Use 0 when unclear, inapplicable, subjective,
about the displayed grid, or when the question merely repeats a fact already in the description.
For example, six red, three blue and one unclear would use [1,1,1,1,1,1,2,2,2,0]
if that is their tile order. Do not invent differences. Output only JSON."""
EXAMPLES = {1078, 2029, 1071, 1540, 853}


def first(row):
    return next((r for r, rank in enumerate(row['ranks']) if rank <= 10), 11)


def sample():
    tasks, controls = {}, {}
    for _, group, refs, root in base.cohorts():
        for task in group:
            i = task['qid']
            tasks[i] = (task, root)
            controls[i] = refs[i]
    rng = random.Random(57872511)
    chosen = []
    for low, high, n in [(1, 3, 8), (4, 10, 6), (11, 11, 6)]:
        candidates = sorted(i for i, row in controls.items() if low <= first(row) <= high and i not in EXAMPLES)
        chosen.extend(rng.sample(candidates, n))
    rng.shuffle(chosen)
    return chosen, tasks, controls


def normalized(q):
    return re.sub(r'^\s*(?:question:\s*)+', '', q.strip(), flags=re.I).casefold().strip()


def parse(raw, n):
    try:
        rows = json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
    except ValueError:
        return None
    if (not isinstance(rows, list) or len(rows) != n or any(not isinstance(row, list) or len(row) != 10
            or any(type(a) is not int or not 0 <= a <= 10 for a in row) for row in rows)):
        return None
    return rows


def split_score(row):
    # Of 45 pairs, count only pairs with two visible, different answers.
    counts = Counter(a for a in row if a)
    total = sum(counts.values())
    return (total * total - sum(n*n for n in counts.values())) // 2


def pick(questions, default, rows):
    if rows is None:
        return default
    scores = list(map(split_score, rows))
    index = next(i for i, q in enumerate(questions) if normalized(q) == normalized(default))
    return default if scores[index] == max(scores) else questions[scores.index(max(scores))]


def run_one(pipe, task, root, control, grid, positions):
    selected, reviews = [], []
    original_reps = base.shared.representatives
    original_choose = pipe.select_question
    def capture(*args, **kwargs):
        selected[:] = original_reps(*args, **kwargs)
        return selected.copy()

    def choose(**kwargs):
        default = original_choose(**kwargs)
        proposals = kwargs['questions']
        unique = {}
        for q in proposals:
            unique.setdefault(normalized(q), q)
        questions = list(unique.values())
        rows, raw = None, None
        if len(questions) > 1:
            base.build_grid([base.REPO/'data'/pipe.search_space[i] for i in selected], grid)
            raw = pipe.client.complete([dict(role='system', content=PROMPT),
                dict(role='user', content=json.dumps(dict(description=kwargs['caption_recon'],
                                                          questions=questions)))], 420, image=grid)
            rows = parse(raw, len(questions))
        question = pick(questions, default, rows)
        reviews.append(dict(default=default, questions=questions, groups=rows, raw=raw, selected=question))
        return question

    base.shared.ROOT = root
    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(base.shared, 'representatives', capture), patch.object(pipe, 'select_question', choose):
        row = base.shared.run_dialogue(pipe, 's3_current_mmr', task, positions[task['img']], hits_only=True)
    for r, (turn, review) in enumerate(zip(row['turns'], reviews)):
        ref = control['turns'][r]
        assert (turn['query_before'], turn['selected_ids'], turn['candidate_questions'], review['default']) == (
            ref['query_before'], ref['selected_ids'], ref['candidate_questions'], ref['question'])
        if turn['question'] != ref['question']:
            break
        assert row['ranks'][r+1] == control['ranks'][r+1] and turn['query_after'] == ref['query_after']
    assert row['ranks'][0] == control['ranks'][0] and len(row['turns']) == len(reviews)
    return dict(row, arm=ARM, reviews=reviews)


def report(ids, controls, rows):
    completed = [i for i in ids if i in rows]
    def curve(source):
        return [sum(first(source[i]) <= k for i in completed) for k in range(11)]
    comparisons = [(first(controls[i]), first(rows[i])) for i in completed]
    reviews = [review for row in rows.values() for review in row['reviews']]
    return dict(n=len(completed), caption_kl=curve(controls), visual=curve(rows),
                gained=[i for i in completed if first(rows[i]) <= 10 < first(controls[i])],
                lost=[i for i in completed if first(controls[i]) <= 10 < first(rows[i])],
                faster=sum(b<a for a,b in comparisons), slower=sum(b>a for a,b in comparisons),
                changed=sum(r['selected'] != r['default'] for r in reviews), turns=len(reviews),
                malformed=sum(r['raw'] is not None and r['groups'] is None for r in reviews))


@torch.inference_mode()
def main(check):
    ids, tasks, controls = sample()
    assert split_score([1]*5+[2]*5) == 25 and split_score([1]*9+[0]) == 0
    assert parse('[[1]]', 1) is None
    assert pick(['a','b'], 'b', [[1]*10]*2) == 'b'
    assert pick(['a','b'], 'a', [[1]*10, [1,2]*5]) == 'b'
    print('20 paired controls verified. IDs:', ids, flush=True)
    if check:
        return
    OUT.mkdir(exist_ok=True)
    with ExitStack() as stack:
        lock = stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = dict(ids=ids, model=base.SNAPSHOT.name, prompt=PROMPT,
                    sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        path = OUT/'plan.json'
        if path.exists() and base.shared.read_json(path) != plan:
            raise ValueError('Output directory contains another pilot plan')
        base.shared.atomic_json(path, plan)
        rows = base.shared.read_records(OUT, ARM)
        assert rows.keys() <= set(ids)
        if len(rows) < len(ids):
            if not torch.backends.mps.is_available():
                raise SystemExit('The matched Qwen pipeline needs Metal. Launch from Mac Terminal.')
            with patch.object(base.mac, '__file__', str(base.GRID_WORKER)):
                worker = base.mac.MacQwen(base.SNAPSHOT, OUT)
            stack.callback(worker.close)
            pipe = base.mac.load_blip(base.INPUTS, worker)
            positions = {name:i for i,name in enumerate(pipe.search_space)}
            grid = Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
            stack.enter_context(patch.object(base.shared, 'history_mmr', partial(base.shared.history_mmr, weight=.85)))
            for i in ids:
                if i in rows:
                    continue
                task, root = tasks[i]
                row = run_one(pipe, task, root, controls[i], grid, positions)
                base.shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json', row)
                rows[i] = row
                print(f"Completed {len(rows)}/20, dialogue {i}, ranks {row['ranks']}", flush=True)
        result = report(ids, controls, rows)
        base.shared.atomic_json(OUT/'summary.json', result)
        print(result, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    main(parser.parse_args().check_only)
