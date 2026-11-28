"""Paired full-dialogue trials with a single prospective intervention and an answer ablation."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import random
from unittest.mock import patch

import torch
import enrichment_evidence as ev
import pixel_fork_policy as policy
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, mac, preflight, shared
from run_a2r_unseen_pilots import paired_summary

OUT = ROOT/'output/mmr-pixel-counterfactuals'
PREVIOUS = ROOT/'output/mmr-grounded-visual-pilots'
NOVEL = """Does the real answer to this question establish a specific new visible fact about the
subject in this photo beyond the supplied earlier dialogue? Return NEW, KNOWN, or UNCLEAR.
Reject guesses, unanswerable questions, paraphrases or confirmations of earlier facts,
and repetitions about the same subject. Use only the supplied dialogue."""
METHODS = ('annotations','retrieval')


def regression():
    cases = [('development',1579,0),('development',1579,1),('confirmation',858,3),
             ('development',176,1),('development',1169,8)]
    results = []
    for stage,i,j in cases:
        row = shared.read_json(PREVIOUS/stage/'pixels_annotations/dialogues'/f'{i}.json')
        history = prefix(row,j)
        slot = row['audit'][j]['slot']
        reason = policy.memory_veto(slot,history)
        if not reason:
            raise ValueError(f'Previously resolved question escaped the guard: {i}, turn {j+1}')
        results.append([i,j+1,reason])
    example = dict(entity='truck',family='color',values=['red','blue'],question='What is the main color of the truck?')
    assert policy.memory_veto(example,['A truck is parked beside a red umbrella.']) is None
    assert policy.memory_veto(example,['A red truck is parked beside an umbrella.'])
    return results


def prepare():
    groups,_,refs = preflight()
    tasks = {t['qid']:(t,root) for _,group,_,root in groups for t in group}
    old = shared.read_json(PREVIOUS/'plan.json')
    used = set(old['development']+old['confirmation'])
    ids = sorted((i for i,r in refs.items() if i not in used and r['ranks'][0]>10),
                 key=lambda i:(refs[i]['ranks'][0],i))
    assert len(ids)==70 and len(used)==40
    rng = random.Random(57872602)
    a,b = [],[]
    for k in range(4):
        group = rng.sample(ids[len(ids)*k//4:len(ids)*(k+1)//4],10)
        a.extend(group[:5])
        b.extend(group[5:])
    rng.shuffle(a)
    rng.shuffle(b)
    reserve=[i for i in ids if i not in set(a+b)]
    rng.shuffle(reserve)
    reserves={'first':reserve[:15],'replication':reserve[15:]}
    assert len(set(a+b+reserve))==70 and all(len(v)==15 for v in reserves.values())
    paths = [Path(__file__),Path(policy.__file__),Path(ev.__file__),GRID_WORKER,Path(mac.__file__),
             Path(shared.__file__),mac.HERE/'generate_dialog.py',INPUTS/'Protocol/visdial_captions.json',
             INPUTS/'Protocol/Search_Space_val_50k.json']
    plan = dict(splits={'first':a,'replication':b},reserve=reserves,excluded=sorted(used),model=SNAPSHOT.name,
                mmr=.85,corpus=50000,rounds=10,policy=policy.BASIC,
                shared_trigger='first baseline state with >=2 pixel omissions filled on a supported distinction (6 known, at least two groups of 2) and a text-grounded alternative; one fork maximum',
                comparison='all IDs including abstentions. One fork per dialogue; continue ordinary MMR from each distinct actual answer.',
                primary='paired immediate Hits@10 vs MMR, corresponding text rule, and blind alternative; cumulative R0-R10 curves for full trajectories',
                mechanism='new observable answer, exact supported label filled from pixels, immediate top-10 hit, matched controls miss, and withholding answer misses under an identically seeded rewrite',
                advancement='in each split at least one verified unique immediate rescue, net immediate hits positive vs all three controls and R10 no lower than all three. Report insufficient exposure if fewer than five pixel/text question differences per split.',
                regressions=regression(),baseline=hashlib.sha256(json.dumps(refs,sort_keys=True).encode()).hexdigest(),
                sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    # Make JSON round trips stable.
    plan['policy']=list(policy.BASIC)
    path=OUT/'plan.json'
    if path.exists() and shared.read_json(path)!=plan:
        raise ValueError('Plan or inputs differ from the frozen pilot. Existing results have been preserved.')
    shared.atomic_json(path,plan)
    return plan,tasks,refs


def prefix(row,j):
    return [row['queries'][0],*(t['question']+' '+t['answer'] for t in row['turns'][:j])]


def pick_fork(audit,ref):
    trace=[]
    for j,t in enumerate(ref['turns']):
        # Pass only information available to the questioner before this turn.
        chosen,obs=policy.choices(audit,prefix(ref,j),t['query_before'],t['selected_ids'],
                                  t['selected_captions'],t['question'],t['candidate_questions'],
                                  f"blind-v1-{ref['qid']}-{j}")
        trace.append(obs)
        if chosen:
            return dict(pivot=j,trace=trace,**chosen)
    return dict(pivot=None,trace=trace)


def continue_from(pipe,task,inputs,ref,j,question,target):
    choose=pipe.select_question
    def select(**kwargs):
        current=choose(**kwargs)
        k=kwargs['round']-1
        if k<=j:
            t=ref['turns'][k]
            assert (kwargs['caption_recon'],kwargs['questions'],current)==(
                t['query_before'],t['candidate_questions'],t['question']), (task['qid'],k,'prefix drift')
        return question if k==j else current

    shared.ROOT=inputs
    pipe.client.next_seed=1021+task['qid']
    with patch.object(pipe,'select_question',select), patch.object(
            shared,'history_mmr',partial(shared.history_mmr,weight=.85)):
        row=shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    assert row['ranks'][:j+1]==ref['ranks'][:j+1] and row['queries'][:j+1]==ref['queries'][:j+1]
    for k in range(j+1):
        assert row['turns'][k]['selected_ids']==ref['turns'][k]['selected_ids']
        if k<j:
            assert row['turns'][k]['answer']==ref['turns'][k]['answer']
    assert row['turns'][j]['question']==question
    return row


def answer_diagnostic(pipe,audit,ref,j,row,target):
    t=row['turns'][j]
    history=prefix(ref,j)
    q=t['question']
    seed=1021+100*ref['qid']+j
    def rewrite(answer):
        pipe.client.next_seed=seed
        query=pipe.reconstruct_dialog([*history,q+' '+answer])
        if query==t['query_before']:
            pipe.client.next_seed=seed
            query=pipe.paraphrase(query)
        vector=torch.nn.functional.normalize(pipe.get_text_features(query),dim=-1)
        order=(vector @ pipe.img_embs.T).flatten().argsort().flip(0)
        return dict(query=query,rank=int(shared.inverse_ranks(order)[target]))
    factual=rewrite(t['answer'])
    masked=rewrite('I cannot tell.')
    novelty=audit.ask(NOVEL,dict(earlier_dialogue=history,question=q,answer=t['answer']),12).strip().upper().rstrip('.')
    return dict(answer=t['answer'],actual_rank=row['ranks'][j+1],factual=factual,
                withheld=masked,novel=novelty)


def run_case(pipe,audit,case,ref,target,folder):
    task,inputs=case
    root=folder/'dialogues'/str(task['qid'])
    path=root/'fork.json'
    if path.exists():
        fork=shared.read_json(path)
    else:
        fork=pick_fork(audit,ref)
        shared.atomic_json(path,fork)
    j=fork['pivot']
    if j is None:
        return dict(pivot=None,rank_before=None,
                    branches={a:dict(ranks=ref['ranks']) for a in ('mmr',*policy.BASIC)})
    picks={'mmr':0,**fork['picks']}
    branches={}
    for arm,idx in picks.items():
        q=fork['questions'][idx]
        # Identical decisions share a complete counterfactual trajectory and diagnostic.
        # Index 0 may also appear as a nominated question with the identical surface text.
        key=hashlib.sha256(q.encode()).hexdigest()[:24]
        path=root/(key+'.json')
        if not path.exists():
            row=ref if q==ref['turns'][j]['question'] else continue_from(
                pipe,task,inputs,ref,j,q,target)
            d=answer_diagnostic(pipe,audit,ref,j,row,target)
            shared.atomic_json(path,dict(q=q,row=row,diagnostic=d))
        saved=shared.read_json(path)
        assert saved['q']==q and saved['row']['qid']==task['qid']
        d=saved['diagnostic']
        linked=False
        if idx:
            obs=fork['trace'][-1]
            c,p=obs['caption_labels'][idx-1],obs['pixel_labels'][idx-1]
            exact=ev.norm(d['answer'])
            slot=obs['slots'][idx-1]
            if exact not in slot['values']:
                answer=audit.ask('Map this actual answer to exactly one allowed label using only its wording. '
                                 'Use unknown for hedging, multiple labels, negation without a unique value, or missing evidence.',
                                 dict(question=q,answer=d['answer'],allowed=slot['values']),18)
                exact=ev.parse_label(answer,slot['values'])
            linked=(exact is not None and obs['pixel_supported'][idx-1] and
                    any(cv is None and pv==exact for cv,pv in zip(c,p)))
        branches[arm]=dict(ranks=saved['row']['ranks'],q=q,linked=linked,**d)
    return dict(pivot=j,rank_before=ref['ranks'][j],branches=branches)


def report(cases):
    result={}
    for method in METHODS:
        pix='pixels_'+method
        controls=['mmr','captions_'+method,'blind_alternative']
        rows={i:s['branches'][pix] for i,s in cases.items()}
        paired={c:paired_summary(rows,{i:s['branches'][c] for i,s in cases.items()}) for c in controls}
        active={i:s['branches'] for i,s in cases.items() if s['pivot'] is not None}
        immediate={}
        for c in controls:
            gains=[i for i,b in active.items() if b[pix]['actual_rank']<=10<b[c]['actual_rank']]
            losses=[i for i,b in active.items() if b[c]['actual_rank']<=10<b[pix]['actual_rank']]
            immediate[c]=dict(gains=gains,losses=losses,net=len(gains)-len(losses),
                              question_changes=sum(b[pix]['q']!=b[c]['q'] for b in active.values()))
        certified=[]
        for i,b in active.items():
            p=b[pix]
            if (p['actual_rank']<=10 and p['linked'] and p['novel']=='NEW'
                    and p['factual']['rank']<=10<p['withheld']['rank']
                    and all(b[c]['actual_rank']>10 for c in controls)):
                certified.append(i)
        exposed=immediate['captions_'+method]['question_changes']
        passes=bool(certified and exposed>=5 and all(
            immediate[c]['net']>0 and paired[c]['variant'][-1]>=paired[c]['control'][-1] for c in controls))
        result[method]=dict(n=len(cases),forks=len(active),immediate=immediate,
                            full_dialogues=paired,verified_immediate_rescues=certified,
                            low_exposure=exposed<5,passes=passes)
    return result


@torch.inference_mode()
def main(check):
    OUT.mkdir(exist_ok=True,parents=True)
    with ExitStack() as session:
        lock=session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan,tasks,refs=prepare()
        print('Verified five obsolete questions, two disjoint groups of 20, and two reserves of 15 from the original 400.',flush=True)
        if check:
            return
        pipe=None
        results={}
        for stage,ids in plan['splits'].items():
            folder=OUT/stage
            cases={}
            schedule=list(ids)
            for i in schedule:
                done=folder/'dialogues'/str(i)/'result.json'
                if done.exists():
                    case=shared.read_json(done)
                else:
                    if pipe is None:
                        if not torch.backends.mps.is_available():
                            raise SystemExit('Launch the live pilot in Mac Terminal with Metal.')
                        with patch.object(mac,'__file__',str(GRID_WORKER)):
                            worker=session.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                        pipe=mac.load_blip(INPUTS,worker)
                        positions={name:k for k,name in enumerate(pipe.search_space)}
                        audit=ev.Audit(pipe,OUT/'cache',REPO/'data',SNAPSHOT.name,shared.atomic_json)
                    case=run_case(pipe,audit,tasks[i],refs[i],positions[tasks[i][0]['img']],folder)
                    shared.atomic_json(done,case)
                cases[i]=case
                print(f"{stage} {len(cases)}/{len(schedule)}; dialogue {i}; fork at {case['pivot']+1 if case['pivot'] is not None else 'no turn'}",flush=True)
                if len(cases)==len(ids):
                    # Decide expansion from assigned questions alone, before reading the next-stage outcomes.
                    for method in METHODS:
                        if sum(case['pivot'] is not None and
                               case['branches']['pixels_'+method]['q']!=case['branches']['captions_'+method]['q']
                               for case in cases.values())<5:
                            schedule.extend(plan['reserve'][stage])
                            print('Using the frozen 15-dialogue reserve because fewer than five questions differed.',flush=True)
                            break
            results[stage]=report(cases)
            shared.atomic_json(folder/'summary.json',results[stage])
        passed=[m for m in METHODS if all(s[m]['passes'] for s in results.values())]
        low=[m for m in METHODS if any(s[m]['low_exposure'] for s in results.values())]
        shared.atomic_json(OUT/'decision.json',dict(qualifying=passed,insufficient_exposure=low))
        print('Qualifying methods:',passed,'; insufficient exposure:',low,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    main(parser.parse_args().check_only)
