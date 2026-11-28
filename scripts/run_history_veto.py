"""Compare exact-repeat and evidence-backed semantic vetoes on original MMR proposals."""
import argparse
from contextlib import ExitStack, closing
from functools import partial
from pathlib import Path
import fcntl
import hashlib
import json
import random
import re
from unittest.mock import patch

import torch
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, shared, preflight
from run_a2r_unseen_pilots import paired_summary
from enrichment_evidence import Audit, json_value

OUT=ROOT/'output/mmr-history-veto'
ARMS=('exact','exact_and_semantic')
PROMPT="""Which proposed questions have a specific answer already established in this target
dialogue? Return a JSON list of entries only for resolved questions. Each entry has numeric
proposal (one-based), evidence_id, and quote (a verbatim substring of that entry's actual
answer or initial description that supplies the answer). For prior turns resolve short answers
by reading the matching prior question. A matching topic, a question's own presupposition,
or an uncertain response is insufficient. Different subjects, attributes, counts, subparts,
broader questions and requests for finer detail stay eligible unless the actual requested value
is known. Do not infer unmentioned details. Return [] when nothing is clearly resolved."""
UNCERTAIN=re.compile(r"\b(unsure|uncertain|unknown|unclear|probably|maybe|possibly|hard to tell|not sure|cannot tell|can't tell|cannot determine|don't know|can't see)\b",re.I)


def norm(s):
    return ' '.join(re.findall(r"[\w']+",s.casefold().replace('’',"'")))


def classify(history,qs,review=None):
    evidence=[dict(id=0,question=None,answer=history[0])]
    rejected={}
    for line in history[1:]:
        q,sep,a=line.partition('?')
        if not sep or not a.strip() or UNCERTAIN.search(a):
            continue
        item=dict(id=len(evidence),question=q+'?',answer=a.strip())
        evidence.append(item)
        for i,proposal in enumerate(qs):
            if norm(q)==norm(proposal):
                rejected[i]=dict(kind='exact',evidence=item)
    if review is None or len(rejected)==len(qs):
        return rejected
    open_ids={str(i+1):q for i,q in enumerate(qs) if i not in rejected}
    raw=review(PROMPT,dict(dialogue=evidence,proposals=open_ids),420)
    rows=json_value(raw)
    if not isinstance(rows,list):
        # Retry with explicit output format. Avoid silently recording a broken judge as a no-op.
        rows=json_value(review(PROMPT+' Return the JSON array only, without commentary.',
                               dict(dialogue=evidence,proposals=open_ids),512))
        if not isinstance(rows,list):
            raise ValueError('Reviewer returned malformed JSON twice')
    for row in rows:
        if not isinstance(row,dict): continue
        i,k,quote=(row.get(key) for key in ('proposal','evidence_id','quote'))
        if (type(i) is int and str(i) in open_ids and type(k) is int and 0<=k<len(evidence)
                and isinstance(quote,str) and norm(quote) and
                (' '+norm(quote)+' ') in (' '+norm(evidence[k]['answer'])+' ')):
            rejected[i-1]=dict(kind='semantic',evidence=evidence[k],quote=quote)
    return rejected


def simulate(pipe,task,inputs,ref,target,arm,review):
    ordinary,generate=pipe.select_question,shared.question_candidates
    audit=[]
    history=[]
    def proposals(model,dialogue,query,captions):
        history[:]=dialogue
        return generate(model,dialogue,query,captions)
    def choose(**kw):
        qs=kw['questions']
        incumbent=ordinary(**kw)
        veto=classify(history,qs,review if arm=='exact_and_semantic' else None)
        kept=[q for i,q in enumerate(qs) if i not in veto]
        changed=incumbent not in kept and bool(kept)
        q=ordinary(**{**kw,'questions':kept}) if changed else incumbent
        audit.append(dict(incumbent=incumbent,asked=q,veto=veto,all_rejected=not kept))
        return q
    shared.ROOT=inputs
    pipe.client.next_seed=1021+task['qid']
    with patch.object(shared,'history_mmr',partial(shared.history_mmr,weight=.85)), patch.object(
            shared,'question_candidates',proposals),patch.object(pipe,'select_question',choose):
        row=shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    assert row['ranks'][0]==ref['ranks'][0]
    for j,(t,a) in enumerate(zip(row['turns'],audit)):
        old=ref['turns'][j]
        assert (t['query_before'],t['candidate_questions'],t['selected_ids'],a['incumbent'])==(
            old['query_before'],old['candidate_questions'],old['selected_ids'],old['question'])
        if t['question']!=old['question']: break
        assert t['query_after']==old['query_after'] and row['ranks'][j+1]==ref['ranks'][j+1]
    return dict(row,arm=arm,audit=audit)


@torch.inference_mode()
def main(check):
    OUT.mkdir(exist_ok=True,parents=True)
    with ExitStack() as session:
        lock=session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        groups,_,refs=preflight()
        tasks={t['qid']:(t,inputs) for _,group,_,inputs in groups for t in group}
        ids=sorted((i for i in refs if refs[i]['ranks'][0]>10),key=lambda i:(refs[i]['ranks'][0],i))
        rng=random.Random(57872604)
        chosen=[i for j in range(4) for i in rng.sample(ids[len(ids)*j//4:len(ids)*(j+1)//4],10)]
        # Each half has five dialogues from each initial-rank quartile.
        splits=[[i for j,i in enumerate(chosen) if j%10//5==k] for k in range(2)]
        plan=dict(splits=splits,arms=ARMS,model=SNAPSHOT.name,mmr=.85,proposals=5,corpus=50000,
                  rounds=10,primary='paired R10 cumulative Hits@10; earlier first hits as secondary',
                  controls=hashlib.sha256(json.dumps([refs[i] for i in chosen],sort_keys=True).encode()).hexdigest(),
                  source=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        plan['arms']=list(ARMS)
        path=OUT/'plan.json'
        if path.exists() and shared.read_json(path)!=plan: raise ValueError('Saved pilot plan differs')
        shared.atomic_json(path,plan)
        assert len(set(chosen))==40
        print('Verified two groups of 20 initially missed dialogues.',flush=True)
        if check:return
        pipe=None
        for stage,ids in enumerate(splits,1):
            folder=OUT/f'group{stage}'
            completed={}
            for arm in ARMS:
                rows=shared.read_records(folder,arm)
                assert rows.keys()<=set(ids)
                for i in ids:
                    if i in rows:continue
                    if pipe is None:
                        if not torch.backends.mps.is_available():
                            raise SystemExit('Launch the live pilot from Mac Terminal with Metal.')
                        with patch.object(mac,'__file__',str(GRID_WORKER)):
                            worker=session.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                        pipe=mac.load_blip(INPUTS,worker)
                        review=Audit(pipe,OUT/'cache',ROOT,SNAPSHOT.name,shared.atomic_json).ask
                        positions={p:k for k,p in enumerate(pipe.search_space)}
                    task,inputs=tasks[i]
                    rows[i]=simulate(pipe,task,inputs,refs[i],positions[task['img']],arm,review)
                    shared.atomic_json(folder/arm/'dialogues'/f'{i}.json',rows[i])
                    print(f'Group {stage}, {arm}: {len(rows)}/20',flush=True)
                completed[arm]=rows
                shared.atomic_json(folder/arm/'summary.json',paired_summary(rows,refs))
            pair=paired_summary(completed[ARMS[1]],completed[ARMS[0]])
            shared.atomic_json(folder/'reviewer_vs_exact.json',pair)
            print('R10 MMR / exact / reviewer:',sum(min(refs[i]['ranks'])<=10 for i in ids),
                  pair['control'][-1],pair['variant'][-1],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    main(parser.parse_args().check_only)
