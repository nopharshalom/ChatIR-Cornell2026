"""Paper-based A2R policy with the local Qwen, BLIP and matched MMR controls."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
from pathlib import Path
import random
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch
from torch.nn.functional import normalize
import run_a2r_unseen_pilots as previous
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, build_grid, mac, preflight, shared, REPO
from run_matan_visual_challenge import GRID_REF
from run_matan_repaired_pilot import parse_question

OUT = ROOT/'output/qwen-a2r-dpp'
ARM = 'a2r_qwen'
PROMPT = """Inspect the candidate photos in the grid and compare visible differences.
Generate five distinct attribute-value questions for a person who sees only their own photo.
Use the current description and conversation. Ask about observable differences whose answers
are still unknown. Each question must make sense for a single photograph. Do not identify
or reference grid cells. Avoid paraphrases of banned questions. Return a JSON array of
standalone questions, each ending with a question mark."""


def greedy_dpp(vectors, relevance, k=10):
    features = normalize(vectors.detach().cpu().double(),dim=1)*relevance.detach().cpu().double()[:,None]
    kernel = features @ features.T
    residual = kernel.diag().clone()
    factors = kernel.new_zeros(k,len(residual))
    used = torch.zeros(len(residual),dtype=torch.bool)
    picks = []
    for j in range(k):
        index = int(residual.masked_fill(used,-torch.inf).argmax())
        if residual[index] <= 1e-14:
            # Rank-deficient tail: equal zero determinants, retain original relevance order.
            picks.extend(torch.where(~used)[0][:k-j].tolist())
            break
        picks.append(index)
        used[index] = True
        row = (kernel[index]-factors[:j,index]@factors[:j])/residual[index].sqrt()
        factors[j] = row
        residual = (residual-row.square()).clamp_min(0)
    return picks


def parse(raw):
    raw = re.sub(r'^```(?:json)?\s*|\s*```$','',raw.strip())
    try:
        items = json.loads(raw)
    except ValueError:
        items = raw.splitlines()
    if isinstance(items,str):
        items=[items]
    if not isinstance(items,list):
        return []
    out = []
    for item in items:
        if isinstance(item,str):
            q = parse_question(re.sub(r'^\s*(?:[-*]|\d+[.)])\s*','',item).strip(' ",'))
            if q and not GRID_REF.search(q):
                out.append(q)
    return out


def grid_questions(client,history,query,grid,banned):
    questions=[]
    rejected={previous.label(q) for q in banned}
    def collect(raw):
        for q in parse(raw):
            key=previous.label(q)
            if key not in rejected:
                questions.append(q)
                rejected.add(key)
            if len(questions)==5:
                break

    # Retain the ordinary sampling path before attempting directed repairs.
    for _ in range(4):
        data=dict(description=query,dialogue=history,banned=banned+questions)
        raw=client.complete([dict(role='system',content=PROMPT),dict(role='user',content=json.dumps(data))],
                            300,temperature=.5,top_p=.95,image=grid)
        collect(raw)
        if len(questions)==5:
            return questions,0

    # Distinct prompts prevent identical short batches from exhausting the retry budget.
    aspects=('spatial relationship','background landmark','pose or orientation','surface or terrain',
             'equipment detail','visible action','number of objects','marking or pattern',
             'nearby object','clothing detail','setting','foreground detail')
    for attempt,aspect in enumerate(aspects,1):
        data=dict(description=query,dialogue=history,do_not_repeat=banned+questions,
                  suggested_aspect=aspect)
        raw=client.complete([
            dict(role='system',content='Look at the grid. Write exactly ONE new question about an observable '
                 'difference among plausible photos. Try the suggested aspect if applicable. Ask the viewer '
                 'of one photo for the attribute value. Avoid already established facts and listed questions. '
                 'Return only the question ending in a question mark.'),
            dict(role='user',content=json.dumps(data))],64,temperature=.5,top_p=.95,image=grid)
        collect(raw)
        if len(questions)==5:
            return questions,attempt
    raise ProposalShortfall(f'Only {len(questions)}/5 eligible questions after targeted repairs.')


class ProposalShortfall(RuntimeError):
    pass


def run_one(pipe,task,inputs,target,ref,grid,answer):
    selected,trace,banned = [],[],[]
    context={}
    def ranking(arm,queries,history,order,budget):
        context['query']=queries[-1]
        return order,order[:50]

    def represent(arm,pool,ranks,vectors,cluster):
        query=normalize(pipe.get_text_features(context['query']),dim=-1)
        embeddings=vectors[pool]
        relevance=(query @ normalize(embeddings,dim=-1).T).flatten()
        selected[:]=pool.cpu()[greedy_dpp(embeddings,relevance)].tolist()
        return selected.copy()

    def generate(model,history,query,captions):
        build_grid([REPO/'data'/pipe.search_space[i] for i in selected],grid)
        questions,repairs=grid_questions(pipe.client,history,query,grid,banned)
        context['repair_calls']=repairs
        return questions

    def choose(**kw):
        qs=kw['questions']
        weights=kw['cossim_prev'][selected].detach().cpu().double().softmax(0).tolist()
        matrix=[[answer(pipe,i,q) for i in selected] for q in qs]
        scores=[previous.information_gain(row,weights) for row in matrix]
        chosen=qs[max(range(5),key=scores.__getitem__)]
        banned.extend(q for q,g in zip(qs,scores) if q==chosen or g<=1e-12)
        trace.append(dict(ids=selected.copy(),weights=weights,questions=qs,answers=matrix,
                          gain_bits=scores,chosen=chosen,repair_calls=context['repair_calls']))
        return chosen

    shared.ROOT=inputs
    pipe.client.next_seed=1021+task['qid']
    with patch.object(shared,'rankings',ranking), patch.object(shared,'representatives',represent), patch.object(
            shared,'question_candidates',generate), patch.object(pipe,'select_question',choose):
        row=shared.run_dialogue(pipe,ARM,task,target,hits_only=True)
    assert row['ranks'][0]==ref['ranks'][0] and len(trace)==len(row['turns'])
    for t,entry in zip(row['turns'],trace):
        assert len(t['pool_ids'])==50 and len(set(entry['ids']))==10 and set(entry['ids'])<=set(t['pool_ids'])
    return dict(row,a2r_trace=trace)


@torch.inference_mode()
def main(check):
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        lock=stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        groups,_,refs=preflight()
        tasks={t['qid']:(t,inputs) for _,group,_,inputs in groups for t in group}
        eligible=sorted((i for i in refs if refs[i]['ranks'][0]>10),key=lambda i:(refs[i]['ranks'][0],i))
        rng=random.Random(57872603)
        ids=[i for k in range(4) for i in rng.sample(eligible[len(eligible)*k//4:len(eligible)*(k+1)//4],5)]
        rng.shuffle(ids)
        plan=dict(ids=ids,model=SNAPSHOT.name,seed=57872603,dpp_pool=50,dpp_count=10,proposals=5,
                  rounds=10,corpus=50000,baseline='current-query MMR .85 captions',paper='10.1145/3805622.3810597',
                  assumptions='Local grid prompt, normalized exact answer buckets, unit softmax temperature, zero gain and asked questions banned; shared target oracle and query reconstruction.',
                  source={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [
                      Path(__file__),Path(previous.__file__),GRID_WORKER,Path(shared.__file__),Path(mac.__file__)]},
                  control=hashlib.sha256(json.dumps([refs[i] for i in ids],sort_keys=True).encode()).hexdigest())
        planfile=OUT/'plan.json'
        if planfile.exists() and shared.read_json(planfile)!=plan:
            raise ValueError('Saved experiment plan differs')
        shared.atomic_json(planfile,plan)
        print('Verified 20 initially missed dialogues with matched archived MMR controls.',flush=True)
        if check:
            return
        rows=shared.read_records(OUT,ARM)
        assert rows.keys()<=set(ids)
        failures={}
        pipe=None
        stack.enter_context(patch.object(previous,'OUT',OUT))
        for i in ids:
            if i in rows:
                continue
            if pipe is None:
                if not torch.backends.mps.is_available():
                    raise SystemExit('Live Qwen requires Metal. Launch this command in Mac Terminal.')
                with patch.object(mac,'__file__',str(GRID_WORKER)):
                    worker=stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                pipe=mac.load_blip(INPUTS,worker)
                positions={name:k for k,name in enumerate(pipe.search_space)}
                grid=Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
            task,inputs=tasks[i]
            try:
                rows[i]=run_one(pipe,task,inputs,positions[task['img']],refs[i],grid,previous.candidate_answer)
            except ProposalShortfall as exc:
                failures[i]=str(exc)
                shared.atomic_json(OUT/'incomplete.json',failures)
                print(f'Dialogue {i} incomplete: {exc} Continuing with the remaining IDs.',flush=True)
                continue
            shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json',rows[i])
            shared.atomic_json(OUT/'summary.json',previous.paired_summary(rows,refs))
            print(f'{len(rows)}/20 complete',flush=True)
        shared.atomic_json(OUT/'incomplete.json',failures)
        if failures:
            raise SystemExit(f'{len(rows)}/20 dialogues completed. Comparison is incomplete; see incomplete.json.')
        result=previous.paired_summary(rows,refs)
        shared.atomic_json(OUT/'summary.json',result)
        print('Cumulative Hits@10 counts, rounds 0 through 10',flush=True)
        print('MMR',result['control'],'A2R-Qwen',result['variant'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    main(parser.parse_args().check_only)
