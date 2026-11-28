"""Check saved pilot records, no-repeat screens, control matches and evidence policy."""
from pathlib import Path
import json

PROJECT=Path(__file__).resolve().parents[2]
OUT=PROJECT/'output/history-pixels-loop'
def read(p): return json.loads(p.read_text())

def main():
    corpus=read(PROJECT/'tmp/agentir-local-9b-inputs/Protocol/Search_Space_val_50k.json')
    positions={name:i for i,name in enumerate(corpus)}
    controls={r['qid']:r['filtered'] for r in read(OUT/'controls/mmr85.json')['rows']}
    split=read(OUT/'split.json')
    assert len(set(split['development_ids'])|set(split['confirmation_ids']))==40
    for group in ('development','confirmation'):
        for summary in sorted((OUT/group).glob('*/summary.json')):
            folder=summary.parent
            arm=folder.name
            ids=split[group+'_ids']
            assert read(folder/'plan.json')['ids']==ids
            ds=[read(folder/'dialogues'/f'{i}.json') for i in ids]
            assert len(ds)==len(list((folder/'dialogues').glob('*.json')))==20
            for i,d in zip(ids,ds):
                target=positions[d['target']]
                assert i==d['qid'] and arm==d['arm'] and min(d['first'],10)==len(d['turns'])
                shown=set()
                for j,s in enumerate(d['screens']):
                    screen=set(s['display'])
                    assert s['round']==j and len(screen)==len(s['display'])==10 and shown.isdisjoint(screen)
                    assert (target in screen)==(s['rank']<=10)==(d['first']==j)
                    if target in screen: assert s['display'][s['rank']-1]==target
                    shown.update(screen)
                    if s['check']:
                        a=s['check']
                        for label in a['labels']:
                            data=read(OUT/'candidate-cache'/label['file'])
                            assert data['key'][1:4]==[arm,label['id'],corpus[label['id']]]
                            assert data['key'][4:6]==[a['question'],a['answer']] and data['color']==label['color']
                        if a.get('swap'):
                            bad,good=a['swap']
                            assert set(s['ordinary_screen'])-screen=={bad} and screen-set(s['ordinary_screen'])=={good}
                            labels={x['id']:x['color'] for x in a['labels']}
                            assert labels[good]==a['answer'] and labels[bad] and labels[bad]!=a['answer']
                    if j<len(d['turns']):
                        t=d['turns'][j]
                        assert len(set(t['evidence']))==len(t['evidence'])==len(t['candidates'])*2==10
                        assert t['question'] in t['candidates'] and t['answer'] and t['rewrite']==d['screens'][j+1]['query']
                        if arm=='unseen': assert shown.isdisjoint(t['evidence'])
                        if arm=='contrast': assert len(shown.intersection(t['evidence']))<=2
                        if arm.startswith('q-') and t['review']:
                            assert t['review']['selected']==t['question'] and t['review']['default']==t['default']
                            for entry in t['review']['scores'].values():
                                cache=OUT/('q-cache-'+arm)/'answers'/entry['file']
                                data=read(cache)
                                assert data['answers']==entry['answers'] and data['request'][1]==t['evidence']
                if arm=='ordinary': assert d['first']==controls[i]
            hits=[sum(d['first']<=j for d in ds) for j in range(11)]
            assert hits==read(summary)['hits'] and sum(hits[1:6])/100==read(summary)['primary']
            print(group,arm,'20 trajectories verified')
if __name__=='__main__': main()
