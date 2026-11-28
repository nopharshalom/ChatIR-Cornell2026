"""Choose among the five ordinary MMR questions using caption-supported answer groups."""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
import fcntl
from functools import partial
from itertools import combinations
import json
from pathlib import Path
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

from enrichment_evidence import json_value, norm, transfer_score
from guard_history_veto import guard_metal_timeout
from run_candidate_caption_enrichment import OUT as ENRICH, comparison, detail
from run_matan_400 import INPUTS, ROOT, SNAPSHOT, mac, preflight, shared
from run_proxy_retrieval import WORKER, digest, isolated_rng

OUT = ROOT/'output/caption-split-selector'
ARMS = ('original_captions', 'pixel_enriched_captions')
PROMPT = """Read the candidate captions independently. For the given question, group candidates
by explicitly supported answers. Return only a JSON object mapping short, distinct answer
labels to arrays of 1-based caption numbers. Merge synonyms. Use the same referent throughout.
Omit missing, ambiguous or conflicting answers. Absence of a mention is never a negative.
For example {"green":[1,3],"black":[2,4]}. Return {} for an inapplicable, already answered
or repeated question in the user dialogue, or if there are no supported distinctions."""


def labels_from(raw, count):
    obj = json_value(raw)
    labels = [None] * count
    if not isinstance(obj, dict):
        return labels
    for answer, indices in obj.items():
        label = norm(answer)
        if label in {'', 'unknown', 'unclear', 'unsure', 'other', 'not mentioned', 'n a'}:
            continue
        if len(label.split()) > 10 or not isinstance(indices, list):
            return [None] * count
        for index in indices:
            if type(index) is not int or not 1 <= index <= count or labels[index-1] is not None:
                return [None] * count
            labels[index-1] = label
    return labels


def assess(pipe, history, query, question, captions, photos, prior):
    payload = dict(dialogue=history, question=question,
                   captions={str(j):s for j,s in enumerate(captions,1)})
    raw = pipe.client.complete([dict(role='system',content=PROMPT),
                                dict(role='user',content=json.dumps(payload))], 210)
    labels = labels_from(raw, len(captions))
    groups = {label:n for label,n in Counter(labels).items() if label and n >= 2}
    record = dict(question=question, labels=labels, response=raw, score=0.0)
    if len(groups) < 2:
        return record
    shifts = {}
    for answer in groups:
        vector = normalize(pipe.get_text_features(f'{query} {question} {answer}'), dim=-1)
        shifts[answer] = ((vector @ photos.T).flatten() - prior).cpu().tolist()
    transfer = transfer_score(labels, shifts)
    # Fraction of all candidate pairs explicitly separated by replicated groups.
    separated = sum(a*b for a,b in combinations(groups.values(),2))
    coverage = 2*separated / (len(labels)*(len(labels)-1))
    record.update(coverage=coverage, transfer=transfer, score=coverage*max(0, 2*transfer-1))
    return record


def play(pipe, arm, task, folder, target, cache):
    representatives, generate, select = shared.representatives, shared.question_candidates, pipe.select_question
    state = {}
    trace = []

    def capture_ids(*args):
        ids = representatives(*args)
        state['ids'] = list(ids)
        return ids

    def proposals(model, history, query, captions):
        state.update(history=history.copy(), captions=captions.copy())
        return generate(model, history, query, captions)

    def choose(**kw):
        ordinary = select(**kw)
        ids, captions = state['ids'], state['captions']
        if len(kw['questions']) != 5 or captions != [pipe.captions[i]['caption'][0] for i in ids]:
            raise ValueError('Unaligned ordinary questions or evidence')
        with isolated_rng(pipe.client):
            if arm == 'pixel_enriched_captions':
                captions = [detail(pipe, 'pixels', i, cache)['enriched'] for i in ids]
            pipe.client.next_seed = None
            assessments = [assess(pipe, state['history'], kw['caption_recon'], q, captions,
                                  pipe.img_embs[ids], kw['cossim_prev'][ids])
                           for q in dict.fromkeys(kw['questions'])]
        best = max(d['score'] for d in assessments)
        options = [d['question'] for d in assessments if d['score'] == best]
        choice = ordinary if ordinary in options else options[0]
        trace.append(dict(ordinary_choice=ordinary, assessments=assessments, choice=choice))
        return choice

    with patch.object(shared,'ROOT',folder), patch.object(shared,'representatives',capture_ids), \
            patch.object(shared,'question_candidates',proposals), patch.object(pipe,'select_question',choose):
        pipe.client.next_seed = 1021 + task['qid']
        row = shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    return dict(row, arm=arm, selection_trace=trace)


def validate(row, ref):
    if (row['target'],row['queries'][0],row['ranks'][0]) != (ref['target'],ref['queries'][0],ref['ranks'][0]):
        raise ValueError('Unmatched initial retrieval')
    for t,d in zip(row['turns'],row['selection_trace'],strict=True):
        if len(t['candidate_questions']) != 5 or d['choice'] != t['question'] or t['question'] not in t['candidate_questions']:
            raise ValueError('Question selection drift')


@torch.inference_mode()
def run(check):
    groups,_,refs = preflight()
    tasks = {t['qid']:(t,folder) for _,group,_,folder in groups for t in group}
    splits = shared.read_json(ENRICH/'plan.json')['splits']
    ids = [i for half in splits for i in half]
    if list(map(len,splits)) != [20,20] or len(set(ids)) != 40 or any(refs[i]['ranks'][0]<=10 for i in ids):
        raise ValueError('Unexpected pilot cohort')
    plan = dict(splits=splits, arms=list(ARMS), model=SNAPSHOT.name, weight=.85, prompt=PROMPT,
                controls=digest([refs[i] for i in ids]), source=digest(Path(__file__).read_text()),
                enrichment_sources=shared.read_json(ENRICH/'plan.json')['sources'])
    if check:
        print('Verified both question-selection pilots on the frozen 40 IDs.',flush=True)
        return
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        lock = stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path) != plan:
            raise ValueError('Incompatible caption-split checkpoints')
        shared.atomic_json(path,plan)
        rows = {a:shared.read_records(OUT,a) for a in ARMS}
        for data in rows.values():
            if not data.keys() <= set(ids):
                raise ValueError('Unexpected completed IDs')
            for i,row in data.items():
                validate(row,refs[i])
        stack.enter_context(patch.object(shared,'history_mmr',partial(shared.history_mmr,weight=.85)))
        pipe = None
        cache = {}
        for i in ids:
            for arm,data in rows.items():
                if i in data:
                    continue
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Launch in Mac Terminal for Metal access.')
                    with patch.object(mac,'__file__',str(WORKER)):
                        worker = stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                    pipe = mac.load_blip(INPUTS,worker)
                    positions = {name:j for j,name in enumerate(pipe.search_space)}
                task,folder = tasks[i]
                row = play(pipe,arm,task,folder,positions[task['img']],cache)
                validate(row,refs[i])
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json',row)
                data[i] = row
                print(f'{arm}: {len(data)}/40 saved',flush=True)
            shared.atomic_json(OUT/'summary.json',{
                a:dict(all=comparison(data,refs,ids), halves=[comparison(data,refs,h) for h in splits],
                       choices_changed=sum(t['choice']!=t['ordinary_choice'] for row in data.values()
                                           for t in row['selection_trace']))
                for a,data in rows.items()})
        if any(set(data)!=set(ids) for data in rows.values()):
            raise RuntimeError('Incomplete question-selection pilots')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
