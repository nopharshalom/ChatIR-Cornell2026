"""First-answer replay: use previous display photos as visual contrasts, with no ID masking."""
import torch
from torch.nn.functional import normalize

from run_probe_pool_sweep import ROOT, INPUTS, shared, cpu_retriever

COHORTS = [('paper', 'paper-original200-mmr85-and-caption', 'current_mmr85'),
           ('additional', 'agentir-mmr-history-visual-200', 'mmr85')]
WEIGHT = 0.1


def adapt(vectors, photos, before, after):
    """Amplify features of old candidates that gained compatibility with the real answer."""
    ids = torch.tensor(photos)
    changes = (after[ids] - before[ids]).argsort()
    direction = vectors[ids[changes[-3:]]].mean(0) - vectors[ids[changes[:3]]].mean(0)
    return after + WEIGHT * (vectors @ direction)


@torch.inference_mode()
def main():
    torch.set_num_threads(4)
    corpus = shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json')
    positions = {image:i for i,image in enumerate(corpus)}
    ids, vectors = torch.load(INPUTS/'cache/corpus_blip.pth', map_location='cpu', weights_only=True)
    assert len(corpus)==len(positions)==50000 and torch.equal(ids,torch.arange(len(corpus)))
    vectors = normalize(vectors, dim=-1)
    retrieve = cpu_retriever()
    records = []
    for group, directory, arm in COHORTS:
        refs = shared.read_records(ROOT/'output'/directory, arm)
        assert len(refs)==200
        active = [r for r in refs.values() if r['ranks'][0]>10]
        for offset in range(0,len(active),12):
            batch = active[offset:offset+12]
            prev = retrieve([r['queries'][0] for r in batch])
            curr = retrieve([r['queries'][1] for r in batch])
            for r, before, after in zip(batch,prev,curr,strict=True):
                order = r['turns'][0]['pool_ids']
                target = positions[r['target']]
                assert target not in order[:10]
                record = dict(qid=r['qid'], group=group)
                scores = dict(baseline=after,
                              displayed=adapt(vectors,order[:10],before,after),
                              undisplayed=adapt(vectors,order[10:20],before,after))
                for method, score in scores.items():
                    record[method] = int((score>score[target]).sum())+1
                assert (record['baseline']<=10)==(r['ranks'][1]<=10)
                records.append(record)
    assert len(records)==len({r['qid'] for r in records})==110
    rows = {}
    for group in ('paper','additional','all'):
        rs=[r for r in records if group=='all' or r['group']==group]
        rows[group]={arm:dict(n=len(rs), hits=sum(r[arm]<=10 for r in rs),
                              gained=[r['qid'] for r in rs if r[arm]<=10<r['baseline']],
                              lost=[r['qid'] for r in rs if r['baseline']<=10<r[arm]])
                     for arm in ('baseline','displayed','undisplayed')}
    shared.atomic_json(ROOT/'output/mmr-answer-guided-feedback/replay.json',
                       dict(weight=WEIGHT, contrast_size=3, summary=rows, dialogues=records))
    print(rows)


if __name__=='__main__':
    main()
