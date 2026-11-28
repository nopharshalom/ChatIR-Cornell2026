"""One targeted visual question per dialogue, paired with the MMR caption baseline."""
import argparse
from contextlib import ExitStack
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch

import run_matan_400 as base
from run_matan_repaired_pilot import parse_question

# Same 20 initially missed targets as the second batch of the routing/restart pilot.
IDS = (754,1650,1508,1782,1874,1542,310,955,670,158,508,814,1411,1820,482,1743,1169,1676,1732,69)
ARM = 'mmr85_one_visual_challenge'
OUT = base.ROOT/'output/matan-one-visual-challenge-20'
SYSTEM = """You see ten possible target photos. The searcher sees only their own photo.
Keep the proposed question if it already asks about a useful difference among plausible photos.
Replace it only if you can point to two plausible tiles with distinctly different visible values
of the same attribute that the proposed question overlooks and that the candidate captions
and previous answers do not already settle. The replacement must ask the searcher for the value
of this attribute about their own picture. Ask about an object grounded in the description.
Use tile indices only as private evidence. Return only KEEP or a JSON object with keys
"tile_a", "value_a", "tile_b", "value_b", "question". Tile indices are integers from 0 to 9."""
GRID_REF = re.compile(r'\b(?:grid|candidate|tile|collage|panel)s?\b|\b(?:image|picture|photo)\s*#?\s*\d|\bwhich (?:of (?:the|these) )?(?:image|picture|photo)s?\b|\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth) (?:image|photo|picture)\b',re.I)


def replacement(raw):
    try:
        item = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
        a, b = item['tile_a'], item['tile_b']
        x, y = item['value_a'], item['value_b']
        question = parse_question(item['question'])
        if (type(a) is int and type(b) is int and a != b and 0 <= a < 10 and 0 <= b < 10
                and isinstance(x,str) and isinstance(y,str) and x.strip() and y.strip()
                and x.strip().casefold() != y.strip().casefold() and question and not GRID_REF.search(question)):
            return question
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def first_challenge(pipe, ref, grid):
    history, reviews = [], []
    for j,t in enumerate(ref['turns']):
        # Only information available immediately before asking this question is exposed.
        base.build_grid([base.REPO/'data'/pipe.search_space[n] for n in t['selected_ids']],grid)
        data = dict(initial=ref['queries'][0],history=history,captions=t['selected_captions'],
                    caption_question=t['question'])
        raw = pipe.client.complete([dict(role='system',content=SYSTEM),
                                    dict(role='user',content=json.dumps(data))],180,image=grid)
        q = replacement(raw)
        if q and q.casefold() in {t['question'].casefold(), *(a['question'].casefold() for a in history)}:
            q = None
        reviews.append(dict(round=j+1,raw=raw,replacement=q))
        if q:
            break
        history.append(dict(question=t['question'],answer=t['answer']))
    return reviews


def run_task(pipe, worker, task, control, target_position, grid):
    reviews = first_challenge(pipe, control, grid)
    if not reviews or not reviews[-1]['replacement']:
        return dict(control,arm=ARM,reviews=reviews)
    pivot = reviews[-1]['round']
    choose = pipe.select_question

    def select(**kwargs):
        round_no = kwargs['round']
        incumbent = choose(**kwargs)
        if round_no <= pivot:
            ref = control['turns'][round_no-1]
            assert (kwargs['caption_recon'],kwargs['questions'],incumbent) == (
                ref['query_before'],ref['candidate_questions'],ref['question']), 'Caption prefix did not reproduce'
        return reviews[-1]['replacement'] if round_no == pivot else incumbent

    worker.next_seed = 1021+task['qid']
    with patch.object(pipe,'select_question',select):
        result = base.shared.run_dialogue(pipe,'s3_current_mmr',task,target_position,hits_only=True)
    assert result['ranks'][:pivot] == control['ranks'][:pivot]
    assert result['turns'][pivot-1]['question'] == reviews[-1]['replacement']
    assert result['turns'][pivot-1]['selected_ids'] == control['turns'][pivot-1]['selected_ids']
    return dict(result,arm=ARM,reviews=reviews)


def report(rows, controls):
    def first(r):
        return next((k for k,n in enumerate(r['ranks']) if n<=10),11)
    summary = {}
    for name, data in [('caption',controls),('challenge',rows)]:
        summary[name]=[sum(first(data[i])<=k for i in IDS) for k in range(11)]
    summary['changed_dialogues']=sum(any(t['replacement'] for t in rows[i]['reviews']) for i in IDS)
    summary['won']= [i for i in IDS if first(rows[i])<=10<first(controls[i])]
    summary['lost']= [i for i in IDS if first(controls[i])<=10<first(rows[i])]
    return summary


@torch.inference_mode()
def main(check):
    tasks={}; controls={}
    for _,group,refs,inputs in base.cohorts():
        for t in group:
            i=t['qid']
            if i in IDS:
                tasks[i]=(t,inputs)
                controls[i]=refs[i]
    assert set(IDS)==set(tasks) and all(controls[i]['ranks'][0]>10 for i in IDS)
    assert replacement('4?') is None
    good=dict(tile_a=1,tile_b=3,value_a='green',value_b='white',question='What color is the countertop?')
    assert replacement(json.dumps(good))==good['question']
    assert replacement(json.dumps(dict(good,question='What color is the counter in image 3?'))) is None
    print('Verified 20 matched controls and response parsing.',flush=True)
    if check:
        return
    if not torch.backends.mps.is_available():
        raise SystemExit('The live Qwen pilot requires Metal. Launch it in Mac Terminal.')
    OUT.mkdir(exist_ok=True)
    with ExitStack() as stack:
        lock=stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=dict(ids=list(IDS),prompt=SYSTEM,model=base.SNAPSHOT.name,
                  script=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        path=OUT/'plan.json'
        if path.exists() and base.shared.read_json(path)!=plan:
            raise ValueError('A different pilot used this output directory.')
        base.shared.atomic_json(path,plan)
        rows=base.shared.read_records(OUT,ARM)
        assert rows.keys() <= set(IDS)
        if len(rows)<len(IDS):
            with patch.object(base.mac,'__file__',str(base.GRID_WORKER)):
                worker=base.mac.MacQwen(base.SNAPSHOT,OUT)
            stack.callback(worker.close)
            pipe=base.mac.load_blip(base.INPUTS,worker)
            positions={name:i for i,name in enumerate(pipe.search_space)}
            grid=Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
            stack.enter_context(patch.object(base.shared,'history_mmr',partial(base.shared.history_mmr,weight=.85)))
            for i in IDS:
                if i in rows:
                    continue
                task,base.shared.ROOT=tasks[i]
                row=run_task(pipe,worker,task,controls[i],positions[task['img']],grid)
                base.shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json',row)
                rows[i]=row
                print(f"{len(rows)}/20, {i}, ranks {row['ranks']}",flush=True)
        result=report(rows,controls)
        base.shared.atomic_json(OUT/'summary.json',result)
        print(result,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    main(parser.parse_args().check_only)
