"""Replay top-200 to top-10 display MMR on the frozen 400 vanilla dialogues."""
import argparse
from contextlib import closing
import fcntl
from unittest.mock import patch

import torch
from transformers import AutoTokenizer

from run_overnight_hits import INPUTS, OLD_BASELINE, OVERNIGHT_OUTPUT, ROOT, SNAPSHOT, mac, shared
from generate_dialog import BlipForRetrieval


@torch.inference_mode()
def main(weight=.85):
    cohorts = shared.read_json(ROOT/'output/matan-questioner-400/plan.json')['cohorts']
    rows = {}
    for group, folder in [('paper', OLD_BASELINE), ('additional', OVERNIGHT_OUTPUT)]:
        saved = shared.read_records(folder, 'vanilla')
        for task in cohorts[group]:
            qid = task['qid']
            row = saved[qid]
            assert qid not in rows and (row['target'], row['queries'][0]) == (task['img'], task['caption'])
            rows[qid] = row
    assert len(rows) == 400
    corpus = shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json')
    positions = {name:i for i,name in enumerate(corpus)}
    index, vectors = torch.load(INPUTS/'cache/corpus_blip.pth', weights_only=True, map_location='cpu')
    assert torch.equal(index, torch.arange(50000)) and len(positions) == len(vectors) == 50000
    snapshot = mac.CACHE/'hub/models--Salesforce--blip-itm-large-coco/snapshots'/shared.BLIP_REVISION
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    model = BlipForRetrieval.from_pretrained(snapshot, local_files_only=True).eval()
    queries = list(dict.fromkeys(q for r in rows.values() for q in r['queries']))
    cache = {}
    for start in range(0,len(queries),16):
        batch = queries[start:start+16]
        tokens = tokenizer(batch, return_tensors='pt', padding=True, truncation=True, max_length=512)
        features = model.get_text_features(input_ids=tokens.input_ids, attention_mask=tokens.attention_mask)
        scores = torch.nn.functional.normalize(features,dim=-1) @ vectors.T
        for q, order in zip(batch, scores.argsort(dim=1).flip(1)):
            pool = order[:200]
            mmr = shared.history_mmr(pool, shared.inverse_ranks(order), vectors, count=10, weight=weight)
            cache[q] = pool.tolist(), mmr
    observed = {}
    rank_discrepancies = []
    for i, row in rows.items():
        target = positions[row['target']]
        turns = []
        for k, (q, rank) in enumerate(zip(row['queries'],row['ranks'])):
            pool, selected = cache[q]
            # Prefer the archived ranking when the original questioner saved at least 200 IDs.
            if k < len(row['turns']) and len(row['turns'][k]['pool_ids']) >= 200:
                archived = row['turns'][k]['pool_ids'][:200]
                if archived != pool:
                    ranks = torch.zeros(len(vectors),dtype=torch.long)
                    ranks[archived] = torch.arange(1,201)
                    selected = shared.history_mmr(torch.tensor(archived), ranks, vectors, count=10, weight=weight)
                    pool = archived
            cpu_rank = pool.index(target)+1 if target in pool else None
            if cpu_rank != (rank if rank <= 200 else None):
                rank_discrepancies.append((i,k,rank,cpu_rank))
            assert (target in pool[:10]) == (rank<=10), (i,k,rank,cpu_rank)
            turns.append(dict(nn_rank=rank, mmr_position=selected.index(target)+1 if target in selected else None,
                              nn_ids=pool[:10], mmr_ids=selected))
        observed[i] = turns
    report = []
    for k in range(11):
        nn = {i for i,t in observed.items() if any(x['nn_rank']<=10 for x in t[:k+1])}
        mmr = {i for i,t in observed.items() if any(x['mmr_position'] for x in t[:k+1])}
        unknown = {i for i,t in observed.items() if len(t)<=k and i not in mmr}
        report.append(dict(round=k,nn=len(nn),mmr_min=len(mmr),mmr_max=len(mmr)+len(unknown),
                           gained=sorted(mmr-nn), unavailable=sorted(unknown),nn_only_observed=sorted(nn-mmr)))
    assert [r['nn'] for r in report] == [290,320,338,349,360,362,363,367,368,372,376]
    out = ROOT/'output/matan-display-mmr-400'
    filename = 'replay.json' if weight == .85 else f'replay-{weight:.2f}.json'
    shared.atomic_json(out/filename,dict(weight=weight,pool=200,display=10,rounds=report,
                                             rank_discrepancies=rank_discrepancies,dialogues=observed))
    for r in report:
        print(r['round'],r['nn'],r['mmr_min'],r['mmr_max'],'observed gained/lost',len(r['gained']),len(r['nn_only_observed']))
    print('Rank discrepancies:',rank_discrepancies)
    print('Continuations:',report[-1]['unavailable'])


def display_ranking(order, embeddings):
    selected = shared.history_mmr(order[:200], shared.inverse_ranks(order), embeddings, count=10, weight=.85)
    ids = torch.tensor(selected, device=order.device)
    keep = torch.ones(len(order), dtype=torch.bool, device=order.device)
    keep[ids] = False
    return torch.cat((ids, order[keep[order]]))


@torch.inference_mode()
def finish(out):
    replay = shared.read_json(out/'replay.json')
    missing = set(replay['rounds'][-1]['unavailable'])
    cohorts = shared.read_json(ROOT/'output/matan-questioner-400/plan.json')['cohorts']
    saved = shared.read_records(out, 'display_mmr85')
    assert saved.keys() <= missing
    if saved.keys() != missing:
        if not torch.backends.mps.is_available():
            raise RuntimeError('The Qwen continuation needs a Metal-enabled Terminal session.')
        with closing(mac.MacQwen(SNAPSHOT, out)) as worker:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {img:i for i,img in enumerate(pipe.search_space)}

            def rankings(arm, queries, history, order, budget):
                assert arm == 'vanilla'
                return display_ranking(order, pipe.img_embs), order[:budget]

            with patch.object(shared, 'rankings', rankings):
                for cohort, tasks in cohorts.items():
                    shared.ROOT = INPUTS if cohort == 'paper' else OVERNIGHT_OUTPUT/'fresh-inputs'
                    for task in tasks:
                        i = task['qid']
                        if i not in missing or i in saved:
                            continue
                        initial = replay['dialogues'][str(i)]
                        assert len(initial) == 1 and initial[0]['mmr_position'] is None
                        worker.next_seed = 1021+i
                        row = shared.run_dialogue(pipe, 'vanilla', task, positions[task['img']], hits_only=True)
                        assert row['ranks'][0] > 10
                        row['arm'] = 'display_mmr85'
                        shared.atomic_json(out/'display_mmr85'/'dialogues'/f'{i}.json', row)
                        saved[i] = row
    assert saved.keys() == missing
    report = []
    for r in replay['rounds']:
        k = r['round']
        recovered = sum(min(saved[i]['ranks'][:k+1])<=10 for i in r['unavailable'])
        hits = r['mmr_min'] + recovered
        assert r['mmr_min'] <= hits <= r['mmr_max']
        report.append(dict(round=k, nn=r['nn'], display_mmr=hits, total=400))
    shared.atomic_json(out/'summary.json', report)
    print(report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--weight', type=float, choices=(.50, .70, .85), default=.85, help='Relevance weight for CPU replay.')
    mode.add_argument('--finish', action='store_true', help='Complete the two missing Qwen histories using Metal.')
    args = parser.parse_args()
    torch.set_num_threads(3)
    if args.finish:
        out = ROOT/'output/matan-display-mmr-400'
        with (out/'finish.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finish(out)
    else:
        main(args.weight)
