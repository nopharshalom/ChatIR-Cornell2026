"""Extract first differing questions, their grounded answers and subsequent rank histories."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]/'output'
CONTROL = ('paper-original200-mmr85-and-caption/current_mmr85',
           'agentir-mmr-history-visual-200/mmr85')

def inspect(out, arm):
    records = []
    for path in sorted((out/arm/'dialogues').glob('*.json')):
        row = json.loads(path.read_text())
        sources = [ROOT/d/'dialogues'/path.name for d in CONTROL]
        ref, = [json.loads(p.read_text()) for p in sources if p.is_file()]
        def first(d):
            return next((j for j,r in enumerate(d['ranks']) if r<=10),None)
        for j, (v,k) in enumerate(zip(row['turns'],ref['turns'])):
            if k['question'] == v['question']:
                assert (k['answer'],k['query_after'],k['selected_ids'],k['candidate_questions']) == (
                    v['answer'],v['query_after'],v['selected_ids'],v['candidate_questions'])
                continue
            assert (k['query_before'],k['selected_ids'],k['candidate_questions']) == (
                v['query_before'],v['selected_ids'],v['candidate_questions'])
            def details(t,d):
                return dict(question=t['question'],answer=t['answer'],rewrite=t['query_after'],
                            ranks=d['ranks'],first_hit=first(d))
            records.append(dict(qid=row['qid'],round=j+1,target=ref['target'],description=k['query_before'],
                                initial=ref['queries'][0],prior_turns=[
                                    [p['question'],p['answer']] for p in ref['turns'][:j]],
                                proposals=k['candidate_questions'],tile_ids=k['selected_ids'],
                                caption_kl=details(k,ref),visual=details(v,row),review=row['reviews'][j]))
            break
    return records

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out',type=Path)
    parser.add_argument('--arm',default='indexed')
    args=parser.parse_args()
    records=inspect(args.out,args.arm)
    (args.out/'divergences.json').write_text(json.dumps(records,indent=2)+'\n')
    for d in records:
        k,v=d['caption_kl'],d['visual']
        print(f"{d['qid']} turn {d['round']} | {k['first_hit']} -> {v['first_hit']} | {k['question']} {k['answer']} | {v['question']} {v['answer']}")
