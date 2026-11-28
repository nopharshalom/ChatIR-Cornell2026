"""One grid and one archived question per call, across three question types."""
import argparse
from contextlib import closing
from itertools import combinations
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_matan_400 as base

# Blind reference labels. None withholds ambiguous photos from accuracy scoring.
CASES = (
    (1894, 1, 'Question: How many couches are in the living room?',
     'Count visible sofas or loveseats. Exclude single-person chairs. Count a sectional as one. Use an integer or unknown.',
     ['1', None, '1', '1', None, '1', '1', None, '1', '1']),
    (1894, 1, 'Question: Does the living room have a television?',
     'Answer yes if a television is visible, no if none is visible, unknown if uncertain.',
     ['no','no','no','no','yes','no','yes',None,'yes','yes']),
    (1820, 4, 'Question: Is there a window in the bathroom containing the single white toilet?',
     'Answer yes if a window is visible, no if none is visible, unknown if uncertain.',
     [None,'no','yes','yes','no','no','no','yes','no','no']),
    (82, 1, 'what is the location of the train track where the short train is traveling?',
     'Use station if a passenger platform is visible, otherwise urban for a built-up area, countryside for fields or woods, or unknown if unclear.',
     ['urban','countryside','station','station',None,'countryside',None,'countryside','station','countryside']),
)


def assess(answers, reference):
    labelled = [i for i,v in enumerate(reference) if v is not None]
    pairs = list(combinations(labelled, 2))
    truth = {(i,j) for i,j in pairs if reference[i] != reference[j]}
    predicted = {(i,j) for i,j in pairs if answers[i] != 'unknown' and answers[j] != 'unknown'
                 and answers[i] != answers[j]}
    return dict(tiles=len(labelled), correct=sum(answers[i]==reference[i] for i in labelled),
                true_splits=len(truth), detected=len(truth & predicted), false_splits=len(predicted - truth))


def decode(raw, allowed):
    try:
        values = json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
    except ValueError:
        return None
    if not isinstance(values,list) or len(values)!=10 or any(type(v) not in (int,str) for v in values):
        return None
    values = [str(v).strip().lower() for v in values]
    if all(v in allowed or (allowed == {'unknown'} and v.isascii() and v.isdigit()) for v in values):
        return values
    return None


def main(check):
    corpus = base.shared.read_json(base.INPUTS/'Protocol/Search_Space_val_50k.json')
    records = []
    with TemporaryDirectory() as tmp:
        for n,(qid,turn,q,instructions,reference) in enumerate(CASES):
            source = base.ROOT/f'output/matan-visual-question-selection-20/mmr85_visual_question_selection/dialogues/{qid}.json'
            row = base.shared.read_json(source)
            ids = row['turns'][turn-1]['selected_ids']
            review = row['reviews'][turn-1]
            idx = review['questions'].index(q)
            groups = review['groups'][idx] if review['groups'] is not None else None
            assert len(set(ids)) == len(reference) == 10
            assert groups is None or len(set(groups)-{0}) <= 1
            grid = Path(tmp)/str(n)/'questioner-grid.png'
            grid.parent.mkdir()
            base.build_grid([base.REPO/'data'/corpus[i] for i in ids],grid)
            records.append((grid, dict(qid=qid,turn=turn,question=q,instructions=instructions,
                                       tile_ids=ids,reference=reference,old_groups=groups)))
        if check:
            assert decode('[1,1,1,1,1,1,1,1,1,1]',{'unknown'}) == ['1']*10
            assert decode('["orange"]',{'unknown','yes','no'}) is None
            for _,r in records:
                ref=r['reference']
                ideal=assess([v or 'unknown' for v in ref],ref)
                assert ideal['false_splits']==0 and ideal['detected']==ideal['true_splits']
                print(r['qid'],r['question'],ideal)
            return
        out = base.ROOT/'output/matan-grid-question-types'
        out.mkdir(exist_ok=True)
        results = []
        with patch.object(base.mac,'__file__',str(base.GRID_WORKER)), closing(base.mac.MacQwen(base.SNAPSHOT,out)) as worker:
            for grid,r in records:
                system = ('Answer the question separately for each numbered photo, in tile order 0 through 9. '
                          + r['instructions'] + ' Return only a JSON list of ten answers.')
                raw = worker.complete([dict(role='system',content=system),dict(role='user',content=r['question'])],128,image=grid)
                if 'integer' in r['instructions']:
                    allowed={'unknown'}
                elif 'platform' in r['instructions']:
                    allowed={'station','urban','countryside','unknown'}
                else:
                    allowed={'yes','no','unknown'}
                answers = decode(raw,allowed)
                score = assess(answers,r['reference']) if answers is not None else None
                results.append(dict(**r,system=system,raw=raw,answers=answers,score=score))
                base.shared.atomic_json(out/'results.json',results)
                print(r['question'],score if score is not None else f'Invalid answer: {raw}',flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    main(parser.parse_args().check_only)
