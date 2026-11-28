"""Six-call paired check for the shirt-color question from dialogue 1914."""
import argparse
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
import run_matan_400 as base

QUESTION = 'what is the color of the shirt worn by the man playing professional tennis on clay courts?'
# Visually checked before fresh inference. Use three clearly different shirts.
REFERENCE = {0: 'blue', 3: 'white', 9: 'red'}


def main(check_only):
    pilot = base.ROOT / 'output/matan-visual-question-selection-20'
    row = json.loads((pilot/'mmr85_visual_question_selection/dialogues/1914.json').read_text())
    review = row['reviews'][0]
    assert review['groups'][review['questions'].index(QUESTION)] == [1]*10
    space = json.loads((base.INPUTS/'Protocol/Search_Space_val_50k.json').read_text())
    paths = [base.REPO/'data'/space[i] for i in row['turns'][0]['selected_ids']]
    with TemporaryDirectory() as work:
        root = Path(work)
        full = root/'questioner-grid.png'
        base.build_grid(paths, full)
        tests = []
        with Image.open(full) as original:
            for i, color in REFERENCE.items():
                region = ((i%5)*320, (i//5)*320, (i%5+1)*320, (i//5+1)*320)
                isolated = Image.new('RGB', original.size, (32,32,32))
                isolated.paste(original.crop(region), region[:2])
                single = root/str(i)/full.name
                single.parent.mkdir()
                isolated.save(single)
                messages = [dict(role='system', content='Look only at the requested numbered tile. Answer with one basic color word for the dominant shirt color, or unknown if unclear.'),
                            dict(role='user', content=f'Tile {i}. {QUESTION}')]
                pair = [('grid', full), ('isolated', single)]
                for condition, image in (pair if i != 3 else reversed(pair)):
                    tests.append((i,color,condition,image,messages))
        print('Verified archived zero split and 3 matched tile pairs.', flush=True)
        if check_only:
            return
        out = base.ROOT/'output/matan-shirt-grid-format-check'
        out.mkdir(exist_ok=True)
        answers = []
        with patch.object(base.mac,'__file__',str(base.GRID_WORKER)), closing(base.mac.MacQwen(base.SNAPSHOT,out)) as worker:
            for i,color,condition,image,messages in tests:
                answer = worker.complete(messages,12,image=image)
                answers.append(dict(tile=i,condition=condition,reference=color,answer=answer))
                print(f'Tile {i} ({color}), {condition}: {answer}', flush=True)
        base.shared.atomic_json(out/'answers.json',answers)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    main(parser.parse_args().check_only)
