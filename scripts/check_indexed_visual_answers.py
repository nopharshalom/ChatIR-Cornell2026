"""Reuse scored grids and request only the three incomplete rows with explicit tile keys."""
import json
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

import run_matan_400 as base
from check_qwen_grid_question_types import assess

OUT = base.ROOT / 'output/matan-indexed-grid-audit'
FORMAT = ('Return a JSON object with exactly one answer for each printed tile ID. '
          'The top row is 0,1,2,3,4 and the bottom row is 5,6,7,8,9. '
          'Use these ten ID strings as keys, including "0" and "9".')

def unique_object(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError('duplicate key')
    return dict(pairs)

def indexed(raw):
    try:
        values = json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip(),
                            object_pairs_hook=unique_object)
    except ValueError:
        return None
    if not isinstance(values, dict) or set(values) != set(map(str, range(10))):
        return None
    if any(type(v) not in (int, str) or not str(v).strip() for v in values.values()):
        return None
    return [str(values[str(i)]).strip().casefold() for i in range(10)]

def main():
    OUT.mkdir(exist_ok=True)
    data = base.ROOT / 'output'
    cases = base.shared.read_json(data/'matan-grid-question-types/results.json')
    shirt = base.shared.read_json(data/'matan-one-question-grid-check/result.json')
    corpus = base.shared.read_json(base.INPUTS/'Protocol/Search_Space_val_50k.json')
    with ExitStack() as stack:
        worker = None
        for i, case in enumerate(cases + [shirt]):
            path = OUT / f'case-{i}.json'
            if path.is_file():
                continue
            raw, prompt = case['raw'], case.get('system', case.get('prompt'))
            answers = indexed(raw) if i != 4 else case['answers']
            reused = answers is not None
            grid = OUT / str(i) / 'questioner-grid.png'
            grid.parent.mkdir(exist_ok=True)
            base.build_grid([base.REPO/'data'/corpus[j] for j in case['tile_ids']], grid)
            if not reused:
                if worker is None:
                    with patch.object(base.mac, '__file__', str(base.GRID_WORKER)):
                        worker = stack.enter_context(closing(base.mac.MacQwen(base.SNAPSHOT, OUT)))
                prompt = 'Answer the question separately for each numbered photo. ' + case['instructions'] + ' ' + FORMAT
                raw = worker.complete([dict(role='system', content=prompt),
                                       dict(role='user', content=case['question'])], 180, image=grid)
                answers = indexed(raw)
            if answers and not reused:
                valid = (lambda v: v == 'unknown' or v.isascii() and v.isdigit()) if i == 0 else (lambda v: v in {'yes','no','unknown'})
                if not all(map(valid, answers)):
                    answers = None
            score = assess(answers, case['reference']) if answers else None
            result = dict(question=case['question'], qid=case['qid'], tile_ids=case['tile_ids'],
                          reference=case['reference'], prompt=prompt, raw=raw, answers=answers,
                          reused=reused, score=score, model=base.SNAPSHOT.name)
            base.shared.atomic_json(path, result)
            print(i, answers, score, flush=True)

if __name__ == '__main__':
    main()
