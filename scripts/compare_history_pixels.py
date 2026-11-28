"""Paired outcome and trace audit for one completed split/arm pair."""
import argparse
from collections import Counter
import json
from pathlib import Path
import random
from statistics import mean

ROOT=Path(__file__).resolve().parents[2]/'output/history-pixels-loop'

def rows(folder):
    plan=json.loads((folder/'plan.json').read_text())
    return {i:json.loads((folder/'dialogues'/f'{i}.json').read_text()) for i in plan['ids']}

def credit(r):
    return sum(r['first']<=i for i in range(1,6))/5

def details(d,j):
    s=d['screens'][j]
    t=d['turns'][j] if j<len(d['turns']) else {}
    return dict(round=j,query=s['query'],screen=s['display'],raw_rank=s['raw_rank'],rank=s['rank'],
                evidence=t.get('evidence'),question=t.get('question'),answer=t.get('answer'),
                rewrite=t.get('rewrite'),first=d['first'],ranks=[s['rank'] for s in d['screens']])

def compare(split,arm,baseline):
    root=ROOT/split
    a,b=rows(root/arm),rows(root/baseline)
    assert a.keys()==b.keys() and len(a)==20
    signs=Counter()
    differences=[]
    effects=[]
    for i in a:
        x,y=a[i],b[i]
        assert x['screens'][0]['display']==y['screens'][0]['display'] and x['target']==y['target']
        for d in (x,y):
            displayed=[k for s in d['screens'] for k in s['display']]
            assert len(displayed)==len(set(displayed))==10*len(d['screens'])
            assert len(d['screens'])==len(d['turns'])+1 and len(d['turns'])==min(d['first'],10)
        signs['earlier' if x['first']<y['first'] else 'later' if x['first']>y['first'] else 'tied']+=1
        effects.append(credit(x)-credit(y))
        item=dict(qid=i,control=y['first'],variant=x['first'])
        for key,section,label in [('display','screens','display'),('evidence','turns','evidence'),('question','turns','question')]:
            j=next((j for j,(u,v) in enumerate(zip(x[section],y[section])) if u[key]!=v[key]),None)
            if j is not None:
                item[label]=dict(variant=details(x,j),control=details(y,j))
        differences.append(item)
    rng=random.Random(57872532)
    boot=sorted(mean(rng.choices(effects,k=len(effects))) for _ in range(10000))
    def totals(ds):
        records=list(ds.values())
        swaps=[(s['filtered_rank'],s['rank']) for r in records for s in r['screens'] if s['check'].get('swap')]
        evidence=[t['evidence'] for r in records for t in r['turns']]
        return dict(hits=[sum(r['first']<=j for r in records) for j in range(11)],
                    questions=sum(len(r['turns']) for r in records),primary=mean(map(credit,records)),
                    displayed=sum(len(r['screens'])*10 for r in records),evidence_slots=sum(map(len,evidence)),
                    unique_evidence_summed_by_dialogue=sum(len({i for t in r['turns'] for i in t['evidence']}) for r in records),
                    verified_screens=sum(bool(s['check']) for r in records for s in r['screens']),
                    swaps=len(swaps),immediate_hits_gained=sum(a>10>=b for a,b in swaps),
                    immediate_hits_lost=sum(a<=10<b for a,b in swaps),wall_seconds=sum(r['seconds'] for r in records))
    improvement=mean(effects)
    summary=dict(baseline=baseline,variant=arm,split=split,control=totals(b),treatment=totals(a),paired=dict(signs),
                 primary_delta=improvement,paired_bootstrap_95=[boot[249],boot[9749]],
                 gained=[i for i in a if a[i]['first']<=10<b[i]['first']],
                 lost=[i for i in a if b[i]['first']<=10<a[i]['first']],
                 per_dialogue=differences)
    path=root/arm/f'against-{baseline}.json'
    path.write_text(json.dumps(summary,indent=2)+'\n')
    print({k:v for k,v in summary.items() if k!='per_dialogue'})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('split')
    p.add_argument('arm')
    p.add_argument('baseline',default='ordinary',nargs='?')
    a=p.parse_args()
    compare(a.split,a.arm,a.baseline)
