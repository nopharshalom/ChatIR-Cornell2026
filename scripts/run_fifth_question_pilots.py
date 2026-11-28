"""Matched caption-only MMR pilots: change at most the fifth ordinary proposal."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

from guard_history_veto import guard_metal_timeout
from run_candidate_caption_enrichment import comparison
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, preflight, shared
from run_matan_repaired_pilot import parse_question
from run_proxy_retrieval import WORKER, digest, isolated_rng
from run_question_diversity import key

OUT = ROOT/'output/fifth-question-pilots'
COMMON = """Read the numbered candidate captions as possibilities. Propose one short, neutral,
standalone question for someone looking only at their own photo. Ask about one visible detail
that is still unknown and relevant to the described scene. Do not mention the candidate set,
give answer options, assume an unconfirmed object, or paraphrase any listed question.
Return NONE if the evidence does not support a useful question. Otherwise return only JSON."""
PROMPTS = {
    'contrast': """Find two captions that describe the same kind of subject but EXPLICITLY
state different values for the same visual attribute. Omission is not a contrast. Ask for the
attribute value. Return {"question":"...?", "indices":[1,2], "quotes":["value one","value two"]}.
Copy one short exact supporting phrase from each caption and use their actual 1-based indices.""",
    'followup': """Use a concrete detail first introduced by the user's latest answer.
If the answer is uncertain, generic, already known, or not worth following up, return NONE.
Use the captions to find a meaningful unresolved distinction about that detail. Ask a question
grounded in that detail. Return {"question":"...?", "anchor":"exact phrase from latest answer"}."""
}


def words(s):
    return ' '.join(re.findall(r"\w+",s.casefold()))


def contains(text, phrase):
    return bool(words(phrase)) and f' {words(phrase)} ' in f' {words(text)} '


def decode(raw, arm, history, captions, ordinary):
    text = raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj,dict) or not isinstance(obj.get('question'),str):
        return None
    q = parse_question(obj['question'])
    if not q or key(q) in {key(s) for s in ordinary}:
        return None
    if arm=='contrast':
        indices, quotes = obj.get('indices'),obj.get('quotes')
        if (not isinstance(indices,list) or len(indices)!=2 or
                not all(type(i) is int and 1<=i<=len(captions) for i in indices) or indices[0]==indices[1]
                or not isinstance(quotes,list) or len(quotes)!=2 or not all(isinstance(s,str) for s in quotes)
                or words(quotes[0])==words(quotes[1]) or not all(
                    contains(captions[i-1],s) for i,s in zip(indices,quotes,strict=True))):
            return None
    else:
        anchor = obj.get('anchor')
        if len(history)<2 or not isinstance(anchor,str):
            return None
        old = [history[0],*(h.partition('?')[2] for h in history[1:-1])]
        if not contains(history[-1].partition('?')[2],anchor) or any(contains(h,anchor) for h in old):
            return None
    return dict(obj,question=q)


def offer(pipe, arm, history, query, captions, ordinary):
    if arm=='followup' and len(history)==1:
        return None
    payload = dict(dialogue=history, current_description=query, captions={
        str(j):s for j,s in enumerate(captions,1)}, ordinary_questions=ordinary)
    with isolated_rng(pipe.client):
        pipe.client.next_seed = 57871128
        raw = pipe.client.complete([dict(role='system',content=COMMON+ '\n'+PROMPTS[arm]),
                                    dict(role='user',content=json.dumps(payload))],164)
        obj = decode(raw,arm,history,captions,ordinary)
        if obj and 'uncertain' in pipe.filter_questions(query,obj['question']):
            return obj
    return None


def play(pipe, arm, task, folder, target, ref):
    trace = []
    generate,select = shared.question_candidates,pipe.select_question

    def proposals(model,history,query,captions):
        ordinary = generate(model,history,query,captions)
        obj = offer(model,arm,history,query,captions,ordinary)
        trace.append(dict(ordinary=ordinary.copy(),proposal=obj))
        return ordinary[:4]+[obj['question']] if obj else ordinary

    def choose(**kw):
        d = trace[-1]
        chosen = select(**kw)
        d['original_choice'] = select(**{**kw,'questions':d['ordinary']}) if d['proposal'] else chosen
        return chosen

    with patch.object(shared,'ROOT',folder), patch.object(shared,'question_candidates',proposals), patch.object(
            pipe,'select_question',choose):
        pipe.client.next_seed = 1021+task['qid']
        row = shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    row.update(arm=arm,proposal_trace=trace)
    validate(row,ref)
    return row


def validate(row,ref):
    if (row['target'],row['ranks'][0],row['queries'][0])!=(ref['target'],ref['ranks'][0],ref['queries'][0]):
        raise ValueError('Unmatched initial retrieval')
    prefix = True
    for j,(t,d) in enumerate(zip(row['turns'],row['proposal_trace'],strict=True)):
        old = d['ordinary']
        expected = old[:4]+[d['proposal']['question']] if d['proposal'] else old
        if len(old)!=5 or expected!=t['candidate_questions'] or t['question'] not in expected:
            raise ValueError('Unexpected fifth-slot change')
        if prefix:
            orig = ref['turns'][j]
            if (t['selected_ids']!=orig['selected_ids'] or old!=orig['candidate_questions']
                    or d['original_choice']!=orig['question']):
                raise ValueError(f'Baseline proposal drift for {row["qid"]}, round {j+1}')
            prefix = t['question']==orig['question']
            if prefix and (t['answer']!=orig['answer'] or t['query_after']!=orig['query_after']
                           or row['ranks'][j+1]!=ref['ranks'][j+1]):
                raise ValueError(f'Baseline response drift for {row["qid"]}, round {j+1}')


def first_offer_replay(pipe,row,photo,target):
    """Offline evaluation after the trajectory: the policy never receives these ranks."""
    history = [row['queries'][0]]
    for j,(t,d) in enumerate(zip(row['turns'],row['proposal_trace'],strict=True)):
        if not d['proposal']:
            history.append(t['question']+' '+t['answer'])
            continue
        old = d['ordinary']
        new = d['proposal']['question']
        ranks = {}
        for question in dict.fromkeys([*old,new]):
            pipe.client.next_seed = 1021+row['qid']+j
            answer = pipe.client.answer(photo,question)
            updated = pipe.reconstruct_dialog([*history,f'{question} {answer}'])
            if updated==t['query_before']:
                pipe.client.next_seed = 1021+row['qid']+j
                updated = pipe.paraphrase(updated)
            vec = normalize(pipe.get_text_features(updated),dim=-1)
            order = (vec @ pipe.img_embs.T).flatten().argsort().flip(0)
            ranks[question] = int(shared.inverse_ranks(order)[target])
        return dict(round=j+1,ranks=ranks,new_only_hit=ranks[new]<=10<min(ranks[q] for q in old),
                    lost_only_hit=ranks[old[-1]]<=10<min(ranks[q] for q in [*old[:4],new]),
                    new_selected=t['question']==new,selected_rank_matches=ranks[t['question']]==row['ranks'][j+1])
    return None


def result(rows,refs,ids,splits):
    traces = [(d,t) for r in rows.values() for d,t in zip(r['proposal_trace'],r['turns'],strict=True) if d['proposal']]
    probes = [r['replay'] for r in rows.values() if r['replay'] is not None]
    return dict(all=comparison(rows,refs,ids),halves=[comparison(rows,refs,s) for s in splits],
                offers=len(traces),chosen=sum(t['question']==d['proposal']['question'] for d,t in traces),
                first_offer_replays=len(probes),new_only_hits=sum(p['new_only_hit'] for p in probes),
                new_only_hits_selected=sum(p['new_only_hit'] and p['new_selected'] for p in probes),
                displaced_only_hits=sum(p['lost_only_hit'] for p in probes),
                selected_replay_rank_matches=sum(p['selected_rank_matches'] for p in probes))


@torch.inference_mode()
def run(check):
    groups,_,refs = preflight()
    tasks = {t['qid']:(t,folder) for _,group,_,folder in groups for t in group}
    splits = shared.read_json(ROOT/'output/candidate-caption-enrichment/plan.json')['splits']
    ids = [i for s in splits for i in s]
    if list(map(len,splits))!=[20,20] or len(set(ids))!=40 or any(refs[i]['ranks'][0]<=10 for i in ids):
        raise ValueError('Expected the frozen 40 initial misses')
    plan = dict(splits=splits,model=SNAPSHOT.name,method='s3_current_mmr',weight=.85,prompts=PROMPTS,
                controls=digest([refs[i] for i in ids]),sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [Path(__file__),WORKER,GRID_WORKER,Path(shared.__file__),Path(mac.__file__),mac.HERE/'generate_dialog.py']})
    print('Verified two separate MMR 0.85 pilots on the same 40 IDs.',flush=True)
    if check:
        return
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        lock=stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=OUT/'plan.json'
        if path.exists() and shared.read_json(path)!=plan:
            raise ValueError('Incompatible fifth-question checkpoints')
        shared.atomic_json(path,plan)
        rows={arm:shared.read_records(OUT,arm) for arm in PROMPTS}
        for data in rows.values():
            if not data.keys()<=set(ids):
                raise ValueError('Unexpected dialogue IDs')
            for i,r in data.items():
                validate(r,refs[i])
        stack.enter_context(patch.object(shared,'history_mmr',partial(shared.history_mmr,weight=.85)))
        pipe=None
        for i in ids:
            for arm,data in rows.items():
                if i in data:
                    continue
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Launch from Mac Terminal for Metal access.')
                    with patch.object(mac,'__file__',str(WORKER)):
                        worker=stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                    pipe=mac.load_blip(INPUTS,worker)
                    positions={img:j for j,img in enumerate(pipe.search_space)}
                task,folder=tasks[i]
                target=positions[task['img']]
                row=play(pipe,arm,task,folder,target,refs[i])
                row['replay']=first_offer_replay(pipe,row,folder/'targets'/task['img'],target)
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json',row)
                data[i]=row
                print(f'{arm}: {len(data)}/40 saved',flush=True)
            shared.atomic_json(OUT/'summary.json',{arm:result(data,refs,ids,splits) for arm,data in rows.items()})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
