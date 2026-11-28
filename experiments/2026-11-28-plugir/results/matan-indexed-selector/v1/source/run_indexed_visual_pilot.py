"""Matched, cached 20-dialogue experiment with keyed single-question visual answers."""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import random
import re
import time
from unittest.mock import patch

import torch
import run_matan_visual_selection as old
from check_indexed_visual_answers import FORMAT, indexed

base = old.base
ROOT = base.ROOT/'output/matan-indexed-selector'
COLORS = 'black white gray red orange yellow green blue purple pink brown'.split()
TRAIN = ['station', 'urban', 'countryside']
ARM = 'indexed'
digest = lambda b: hashlib.sha256(b).hexdigest()

def vocabulary(q):
    q = old.normalized(q)
    if re.search(r'\b(candidate|grid|which image|which photo)\b', q):
        return None
    if re.match(r'(?:what|which) (?:is the (?:dominant |primary |main )?)?colou?r\b', q):
        return COLORS, 'Give the dominant basic color of the item asked about. Choose one of ' + ', '.join(COLORS) + ', or unknown if absent or uncertain.'
    if re.match(r'(?:is|are|does|do) ', q) and ' or ' not in q:
        return ['yes','no'], 'Answer yes or no from visible evidence, or unknown if uncertain or inapplicable.'
    if 'train' in q and ('location' in q or q.startswith('where')):
        return TRAIN, 'Use station if a passenger platform is visible, otherwise urban for a built-up area, countryside for fields or woods, or unknown if unclear.'
    return None

def score(values):
    counts = Counter(v for v in values if v != 'unknown')
    return (sum(counts.values())**2 - sum(n*n for n in counts.values())) // 2

class Selector:
    def __init__(self, worker, corpus, out):
        self.worker, self.corpus, self.out = worker, corpus, out

    def review(self, ids, proposals, default):
        unique = {}
        for q in proposals:
            unique.setdefault(old.normalized(q), q)
        questions = list(unique.values())
        default_key = old.normalized(default)
        specs = {old.normalized(q): vocabulary(q) for q in questions}
        rows = {}
        decision = default
        if specs[default_key] and sum(bool(v) for v in specs.values()) >= 2:
            grid = self.out/'questioner-grid.png'
            built = False
            for q in questions:
                key = old.normalized(q)
                if not specs[key]:
                    continue
                vocab, instruction = specs[key]
                prompt = 'Answer the question separately for each numbered photo. ' + instruction + ' ' + FORMAT
                key_data = [base.SNAPSHOT.name,ids,q,prompt,180]
                cache = ROOT/'answers'/(digest(json.dumps(key_data).encode())+'.json')
                if not cache.exists():
                    if not built:
                        base.build_grid([base.REPO/'data'/self.corpus[i] for i in ids], grid)
                        built = True
                    start = time.perf_counter()
                    raw = self.worker.complete([dict(role='system',content=prompt),dict(role='user',content=q)],180,image=grid)
                    elapsed = time.perf_counter() - start
                    answers = indexed(raw)
                    if answers and not set(answers) <= set(vocab + ['unknown']):
                        answers = None
                    base.shared.atomic_json(cache,dict(request=key_data,raw=raw,answers=answers,seconds=elapsed))
                row = base.shared.read_json(cache)
                assert row['request'] == key_data
                rows[key] = dict(file=cache.name, answers=row['answers'], seconds=row['seconds'],
                                 score=score(row['answers']) if row['answers'] else None)
            scores = {k:v['score'] for k,v in rows.items() if v['score'] is not None}
            if default_key in scores and max(scores.values()) > scores[default_key]:
                decision = next(q for q in questions if scores.get(old.normalized(q)) == max(scores.values()))
        return dict(default=default, questions=questions, selected=decision, scores=rows)

def evaluate(selector, pipe, task, root, ref, target, source=None):
    start = time.perf_counter()
    source = source or ref
    reviews = []
    for t in source['turns']:
        default = source['reviews'][t['round']]['default'] if 'reviews' in source else t['question']
        review = selector.review(t['selected_ids'],t['candidate_questions'],default)
        reviews.append(review)
        if review['selected'] != t['question']:
            break
    else:
        row = dict(source, arm=ARM, reviews=reviews, mode='cached trajectory', cached_from=source['arm'])
        row['pipeline_seconds'] = source.get('pipeline_seconds', source['seconds'])
        row['wall_seconds'] = time.perf_counter()-start
        return row
    # Replay the seeded dialogue from round one to preserve the proposal generator's RNG state.
    reviews = []
    selected = []
    original_reps, original_choose = base.shared.representatives, pipe.select_question
    def capture(*args, **kwargs):
        selected[:] = original_reps(*args, **kwargs)
        return selected.copy()
    def choose(**kwargs):
        default = original_choose(**kwargs)
        review = selector.review(selected, kwargs['questions'], default)
        reviews.append(review)
        return review['selected']
    base.shared.ROOT = root
    pipe.client.next_seed = 1021 + task['qid']
    before = time.perf_counter()
    visual_times = []
    original_review = selector.review
    def timed_review(*args):
        ts = time.perf_counter()
        r = original_review(*args)
        visual_times.append(time.perf_counter()-ts)
        return r
    with patch.object(base.shared,'representatives',capture), patch.object(pipe,'select_question',choose), patch.object(selector,'review',timed_review):
        row = base.shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    for j, t in enumerate(row['turns']):
        orig = ref['turns'][j]
        assert (row['ranks'][j],t['query_before'],t['selected_ids'],t['candidate_questions'],reviews[j]['default']) == (
            ref['ranks'][j],orig['query_before'],orig['selected_ids'],orig['candidate_questions'],orig['question'])
        if t['question'] != orig['question']:
            break
        assert t['answer'] == orig['answer'] and t['query_after'] == orig['query_after'] and row['ranks'][j+1]==ref['ranks'][j+1]
    return dict(row,arm=ARM,reviews=reviews,mode='live dialogue',pipeline_seconds=time.perf_counter()-before-sum(visual_times),
                wall_seconds=time.perf_counter()-start)

def report(ids, controls, rows):
    result = old.report(ids, controls, {i:dict(d,reviews=[dict(r,raw=None) for r in d['reviews']]) for i,d in rows.items()})
    result.pop('malformed')
    result['first_hit'] = {i:[None if old.first(d)==11 else old.first(d) for d in (controls[i],rows[i])] for i in ids}
    result['kl_seconds'] = sum(controls[i]['seconds'] for i in ids)
    scores = [s for d in rows.values() for r in d['reviews'] for s in r['scores'].values()]
    result.update(visual_rows=len(scores), invalid_rows=sum(s['answers'] is None for s in scores),
                  comparable_turns=sum(bool(r['scores']) for d in rows.values() for r in d['reviews']),
                  visual_inference_seconds=sum(s['seconds'] for s in scores),
                  measured_wall_dialogue_seconds=sum(d['wall_seconds'] for d in rows.values()),
                  uncached_dialogue_seconds_estimate=sum(d['pipeline_seconds'] for d in rows.values())+sum(s['seconds'] for s in scores))
    return result

@torch.inference_mode()
def main(split):
    tuning,tasks,controls = old.sample()
    assert tuning == base.shared.read_json(old.OUT/'plan.json')['ids']
    excluded = set(old.EXAMPLES) | set(tuning)
    for folder in ('matan-one-visual-challenge-20','matan-repaired-images-pilot-10'):
        excluded.update(base.shared.read_json(base.ROOT/'output'/folder/'plan.json')['ids'])
    rng = random.Random(57872512)
    holdout = []
    for low, high, n in [(1,3,9),(4,10,5),(11,11,6)]:
        holdout.extend(rng.sample(sorted(i for i,c in controls.items() if i not in excluded and low<=old.first(c)<=high),n))
    rng.shuffle(holdout)
    ids = tuning if split=='tune' else holdout
    out = ROOT/'v1'/split
    out.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        lock=stack.enter_context((ROOT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = dict(ids=ids,confirmation_ids=holdout, model=base.SNAPSHOT.name,
                        mmr=.85, corpus=50000,proposals=5,rounds=10,stop='first rank <= 10',
                        selector='highest supported pair count when KL and a challenger have comparable scores; ties retain KL',
                        source={p.name:digest(p.read_bytes()) for p in [Path(__file__),Path(old.__file__),Path(base.__file__),base.GRID_WORKER,Path(base.shared.__file__),Path(__file__).with_name('check_indexed_visual_answers.py')]},
                        control_sha256={i:digest(json.dumps(controls[i],sort_keys=True).encode()) for i in ids})
        manifest['control_sha256']={str(k):v for k,v in manifest['control_sha256'].items()}
        plan=out/'plan.json'
        if plan.exists() and manifest!=base.shared.read_json(plan):
            raise ValueError('Saved experiment plan differs')
        base.shared.atomic_json(plan,manifest)
        rows=base.shared.read_records(out,ARM)
        if set(ids) != rows.keys():
            with patch.object(base.mac,'__file__',str(base.GRID_WORKER)):
                worker=stack.enter_context(closing(base.mac.MacQwen(base.SNAPSHOT,out)))
            pipe=base.mac.load_blip(base.INPUTS,worker)
            positions={p:i for i,p in enumerate(pipe.search_space)}
            selector=Selector(worker,pipe.search_space,out)
            stack.enter_context(patch.object(base.shared,'history_mmr',partial(base.shared.history_mmr,weight=.85)))
            for i in ids:
                if i in rows:
                    continue
                task,root=tasks[i]
                row=evaluate(selector,pipe,task,root,controls[i],positions[task['img']])
                assert row['ranks'][0]==controls[i]['ranks'][0] and len(row['turns'])==len(row['reviews'])
                assert all(len(t['candidate_questions'])==5 and r['selected']==t['question'] for t,r in zip(row['turns'],row['reviews']))
                base.shared.atomic_json(out/ARM/'dialogues'/f'{i}.json',row)
                rows[i]=row
                print('Saved',len(rows),'of',len(ids),'qid',i,'first hit KL/visual',old.first(controls[i]),old.first(row),flush=True)
        summary=report(ids,controls,rows)
        base.shared.atomic_json(out/'summary.json',summary)
        print(summary,flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('split',choices=['tune','confirm'])
    main(parser.parse_args().split)
