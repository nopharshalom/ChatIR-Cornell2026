"""Paired live/replay pilots with distinct evidence and display histories."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from unittest.mock import patch

import torch
import run_matan_400 as base
import run_indexed_visual_pilot as indexed

OUT = base.ROOT/'output/history-pixels-loop'
SH = base.shared
COLORS = set(indexed.COLORS + ['beige','tan'])
RULE = ("Using only the supplied evidence, answer the question about this candidate. "
        "Return one basic color word only, or unknown if the evidence does not establish the "
        "specified item's color. Do not infer a color from what is typical. "
        "Use " + ', '.join(sorted(COLORS)) + ", or unknown.")
ARMS = ('ordinary','unseen','caption','pixels','contrast','fallback')
sha = lambda b: hashlib.sha256(b).hexdigest()

def color(value):
    v=value.strip().lower().strip('." ').replace('grey','gray')
    return v if v in COLORS else None

def fact(t):
    q=indexed.old.normalized(t['question'])
    if not re.match(r'(what|which) (is the (dominant |primary |main )?)?colou?r\b',q):
        return None
    v=color(t['answer'])
    return (t['question'],v) if v else None

def verify(pipe, image_id, q, answer, arm):
    name=pipe.search_space[image_id]
    evidence=pipe.captions[image_id]['caption'][0] if arm=='caption' else None
    messages=[dict(role='system',content=RULE),
              dict(role='user',content=(f'Caption: {evidence}\n' if evidence is not None else '') + q)]
    key=[base.SNAPSHOT.name,arm,image_id,name,q,answer,messages,12]
    filename=sha(json.dumps(key).encode())+'.json'
    path=OUT/'candidate-cache'/filename
    if not path.exists():
        started=time.perf_counter()
        raw=pipe.client.complete(messages,12,image=base.REPO/'data'/name if arm=='pixels' else None)
        SH.atomic_json(path,dict(key=key,raw=raw,color=color(raw),seconds=time.perf_counter()-started))
    row=SH.read_json(path)
    assert row['key']==key
    return dict(id=image_id,color=row['color'],file=filename,seconds=row['seconds'])

def screen_order(pipe, ordered, last, arm):
    ids=ordered[:10].copy()
    audit={}
    if arm in ('pixels','caption') and last and (f:=fact(last)):
        q,wanted=f
        labels=[verify(pipe,i,q,wanted,arm) for i in ordered[7:13]]
        audit=dict(question=q,answer=wanted,labels=labels)
        bad=next((i for i in reversed(labels[:3]) if i['color'] and i['color']!=wanted),None)
        good=next((i for i in labels[3:] if i['color']==wanted),None)
        if bad and good:
            audit['swap']=[bad['id'],good['id']]
            ids[ids.index(bad['id'])]=good['id']
    return ids,audit

def evidence(pipe, order, ranks, rejected, budget, arm):
    if arm not in ('unseen','contrast'):
        pool=order[:budget]
    else:
        # Enough candidates to refill even if every rejected ID lies at the head.
        head=order[:budget+len(rejected)].tolist()
        pool=order.new_tensor([i for i in head if i not in rejected][:budget])
    selected=SH.history_mmr(pool,ranks,pipe.img_embs,weight=.85)
    if arm=='contrast':
        contrasting=[i for i in order[:budget].tolist() if i in rejected][:2]
        if contrasting:
            selected=selected[:-len(contrasting)] + contrasting
    return selected

def dialogue(pipe, task, root, ref, arm, target, out, live):
    start=time.perf_counter()
    history=[task['caption']]
    query=history[0]
    rejected=set()
    turns=[]
    screens=[]
    shared_seconds=0.
    if live:
        pipe.client.next_seed=1021+task['qid']
    aligned=True
    selector=indexed.Selector(pipe.client,pipe.search_space,out) if arm=='fallback' else None
    for r in range(11):
        vec=torch.nn.functional.normalize(pipe.get_text_features(query),dim=-1)
        scores=(vec @ pipe.img_embs.T).flatten()
        order=scores.argsort().flip(0)
        ranks=SH.inverse_ranks(order)
        head=order[:max(500,13+len(rejected))].tolist()
        if aligned and r<len(ref['ranks']):
            assert query==ref['queries'][r] and int(ranks[target])==ref['ranks'][r], (task['qid'],r,'rank mismatch')
            if r<len(ref['turns']):
                assert head[:10]==ref['turns'][r]['pool_ids'][:10]
        unseen=[i for i in head if i not in rejected]
        before=unseen[:10]
        display,check=screen_order(pipe,unseen,turns[-1] if turns else None,arm)
        assert len(set(display))==10 and rejected.isdisjoint(display)
        # Only this evaluation block reads target position; the ranking policy never receives it.
        target_rank=int(ranks[target])
        rejected_before=sum(int(ranks[j])<target_rank for j in rejected)
        filtered_rank=target_rank-rejected_before
        post=filtered_rank
        if check.get('swap'):
            bad,good=check['swap']
            if target==good: post=before.index(bad)+1
            if target==bad: post=unseen.index(good)+1
        screens.append(dict(round=r,query=query,raw_rank=target_rank,filtered_rank=filtered_rank,
                            rank=post,ordinary_screen=before,display=display,check=check))
        assert (post<=10)==(target in display)
        if post<=10 or r==10:
            break
        rejected.update(display)
        if not live and r>=len(ref['turns']):
            return None  # Diverged past archived stopping point: rerun from the original seed.
        ts=time.perf_counter()
        torch.manual_seed(1021+100*task['qid']+r)
        budget=500-50*r
        ids=evidence(pipe,order,ranks,rejected,budget,arm)
        if not live:
            orig=ref['turns'][r]
            if orig['selected_ids']!=ids:
                return None
            candidates=orig['candidate_questions']
            default=orig['question']
        else:
            captions=[pipe.captions[i]['caption'][0] for i in ids]
            candidates=SH.question_candidates(pipe,history,query,captions)
            default=pipe.select_question(caption_recon=query,questions=candidates,cossim_prev=scores,
                                         k=10,img_embs=pipe.img_embs,threshold=500,round=r+1)
        review=selector.review(ids,candidates,default) if selector else None
        question=review['selected'] if review else default
        if live:
            answer=pipe.client.answer(root/'targets'/task['img'],question)
            history.append(f'{question} {answer}')
            updated=pipe.reconstruct_dialog(history)
            if updated==query:
                updated=pipe.paraphrase(updated)
            elapsed=time.perf_counter()-ts
        else:
            if question!=orig['question']:
                return None
            answer,updated,elapsed=orig['answer'],orig['query_after'],orig['seconds']
            history.append(f'{question} {answer}')
        if aligned and r<len(ref['turns']):
            orig=ref['turns'][r]
            if ids==orig['selected_ids']:
                assert candidates==orig['candidate_questions'] and default==orig['question'], (task['qid'],r,'proposals')
                if question==orig['question']:
                    assert (answer,updated)==(orig['answer'],orig['query_after'])
            aligned=(ids,candidates,question,answer,updated)==(orig['selected_ids'],orig['candidate_questions'],orig['question'],orig['answer'],orig['query_after'])
        else:
            aligned=False
        assert len(candidates)==5 and question in candidates and len(set(ids))==10
        if arm=='unseen':
            assert rejected.isdisjoint(ids)
        turns.append(dict(evidence=ids,candidates=candidates,default=default,question=question,answer=answer,
                          rewrite=updated,review=review,shared_seconds=elapsed))
        shared_seconds+=elapsed
        query=updated
    hit=r if post<=10 else 11
    return dict(qid=task['qid'],target=task['img'],arm=arm,first=hit,turns=turns,screens=screens,
                shared_seconds=shared_seconds,live=live,seconds=time.perf_counter()-start)

def metrics(rows):
    hits=[sum(row['first']<=r for row in rows) for r in range(11)]
    return dict(hits=hits,primary=sum(hits[1:6])/(5*len(rows)),questions=sum(len(row['turns']) for row in rows),
                wall=sum(row['seconds'] for row in rows),live=sum(row['live'] for row in rows),
                shared_seconds=sum(row['shared_seconds'] for row in rows),
                verifier_seconds=sum(x['seconds'] for row in rows for s in row['screens'] for x in s['check'].get('labels',[])),
                swaps=sum('swap' in s['check'] for row in rows for s in row['screens']))

@torch.inference_mode()
def main(split,arms):
    ids=SH.read_json(OUT/'split.json')[split+'_ids']
    assert len(set(ids))==20
    if split=='confirmation':
        freeze=SH.read_json(OUT/'freeze.json')
        assert set(arms)<=set(freeze['arms']) and freeze['source']==sha(Path(__file__).read_bytes())
    refs={}
    for _,tasks,records,root in base.cohorts():
        for task in tasks:
            i=task['qid']
            if i in ids: refs[i]=(task,root,records[i])
    control={row['qid']:row['filtered'] for row in SH.read_json(OUT/'controls/mmr85.json')['rows']}
    out=OUT/split
    out.mkdir(exist_ok=True)
    with ExitStack() as stack:
        lock=stack.enter_context((OUT/'runner.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pipe=None
        for arm in arms:
            folder=out/arm
            plan=dict(ids=ids,arm=arm,model=base.SNAPSHOT.name,source=sha(Path(__file__).read_bytes()),
                      shared_source=sha(Path(SH.__file__).read_bytes()),control_hash={str(i):sha(json.dumps(refs[i][2],sort_keys=True).encode()) for i in ids},
                      split_sha=sha((OUT/'split.json').read_bytes()),mmr=.85,policy='no-repeat top ten',max_questions=10,proposals=5,corpus=50000)
            if (folder/'plan.json').exists(): assert SH.read_json(folder/'plan.json')==plan
            SH.atomic_json(folder/'plan.json',plan)
            rows=[]
            for i in ids:
                path=folder/'dialogues'/f'{i}.json'
                if not path.exists():
                    if pipe is None:
                        with patch.object(base.mac,'__file__',str(base.GRID_WORKER)):
                            worker=stack.enter_context(closing(base.mac.MacQwen(base.SNAPSHOT,out)))
                        pipe=base.mac.load_blip(base.INPUTS,worker)
                        positions={name:i for i,name in enumerate(pipe.search_space)}
                    task,root,ref=refs[i]
                    start=time.perf_counter()
                    args=(pipe,task,root,ref,arm,positions[task['img']],out)
                    row=dialogue(*args,live=False)
                    if row is None:
                        row=dialogue(*args,live=True)
                    if arm=='ordinary': assert row['first']==control[i], (i,'archived no-repeat control')
                    row['seconds']=time.perf_counter()-start
                    SH.atomic_json(path,row)
                rows.append(SH.read_json(path))
                print(arm,len(rows),i,'first visual/control',rows[-1]['first'],control[i],flush=True)
            result=metrics(rows)
            SH.atomic_json(folder/'summary.json',result)
            print(arm,result,flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('split',choices=['development','confirmation'])
    parser.add_argument('arms',nargs='+',choices=ARMS)
    args=parser.parse_args()
    main(args.split,args.arms)
