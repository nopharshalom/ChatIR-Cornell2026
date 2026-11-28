"""Ask one shirt-color question on the archived ten-tile grid in one Qwen call."""
import argparse
from contextlib import closing
from itertools import combinations
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from check_qwen_grid_format import QUESTION, base

# Broad dominant colors, labeled from the original photos before inference.
REFERENCE = ['blue', 'blue', 'blue', 'white', 'blue', 'white', 'blue', 'white', 'blue', 'red']
PALETTE = 'black white gray red orange yellow green blue purple pink brown unknown'.split()
SYSTEM = ('Answer for the tennis player in each numbered tile, in order from 0 through 9. '
          'Give the dominant shirt color. Map shades to the nearest basic color. Choose one of '
          + ', '.join(PALETTE) + '. Return a JSON list of ten color words only.')


def evaluate(raw):
    content = raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:
        answers = json.loads(content)
    except ValueError:
        answers = None
    if isinstance(answers, list) and all(isinstance(v, str) for v in answers):
        answers = [v.strip().lower() for v in answers]
        if len(answers) == 10 and all(v in PALETTE for v in answers):
            different = [(i,j) for i,j in combinations(range(10),2)
                         if 'unknown' not in (answers[i],answers[j]) and answers[i] != answers[j]]
            return dict(valid=True, answers=answers, correct_tiles=sum(a==b for a,b in zip(answers,REFERENCE)),
                        split_pairs=len(different), false_splits=sum(REFERENCE[i]==REFERENCE[j] for i,j in different))
    return dict(valid=False)


def main(check_only):
    f = base.ROOT/'output/matan-visual-question-selection-20/mmr85_visual_question_selection/dialogues/1914.json'
    row = base.shared.read_json(f)
    review = row['reviews'][0]
    assert review['groups'][review['questions'].index(QUESTION)] == [1]*10
    space = base.shared.read_json(base.INPUTS/'Protocol/Search_Space_val_50k.json')
    ids = row['turns'][0]['selected_ids']
    assert len(set(ids)) == 10
    assert evaluate(json.dumps(REFERENCE))['split_pairs'] == 27
    assert evaluate(json.dumps(['unknown']*10))['split_pairs'] == 0
    assert not evaluate('["red"]')['valid']
    with TemporaryDirectory() as work:
        grid = Path(work)/'questioner-grid.png'
        base.build_grid([base.REPO/'data'/space[i] for i in ids], grid)
        if check_only:
            print('Original grid and scorer verified. Reference has 27 distinct-answer pairs out of 45.')
            return
        out = base.ROOT/'output/matan-one-question-grid-check'
        out.mkdir(exist_ok=True)
        with patch.object(base.mac, '__file__', str(base.GRID_WORKER)), closing(base.mac.MacQwen(base.SNAPSHOT,out)) as worker:
            raw = worker.complete([dict(role='system',content=SYSTEM),dict(role='user',content=QUESTION)],96,image=grid)
        result = dict(qid=1914, question=QUESTION, prompt=SYSTEM, tile_ids=ids, raw=raw, reference=REFERENCE,
                      original_split_pairs=0, reference_split_pairs=27, **evaluate(raw))
        base.shared.atomic_json(out/'result.json',result)
        print('Qwen:',raw)
        if result['valid']:
            print(f"Correct colors: {result['correct_tiles']}/10. Distinguished pairs: {result['split_pairs']}/45 "
                  f"(visual reference: 27/45; false splits: {result['false_splits']}).")
        else:
            print('Qwen did not return ten valid color labels; raw answer saved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    main(parser.parse_args().check_only)
