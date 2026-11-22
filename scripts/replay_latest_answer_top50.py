"""Compare latest-answer and fused top-50 display rankings on saved vanilla turns."""
import csv
import json
import time

import torch

from analyze_500_dialogues import PATHS
from run_probe_pool_sweep import INPUTS, ROOT, REPO, cpu_retriever

HERE = REPO / 'analysis/latest-answer-top50'
METHODS = ('vanilla', 'latest', 'fusion')


def within_pool_ranks(base, detail, target):
    # Use vanilla order for ties in either rerank. rank 1 is best.
    order = base.argsort(descending=True, stable=True)
    vanilla_rank = (order == target).nonzero().item() + 1
    if vanilla_rank > 50 or detail is None:
        return dict.fromkeys(METHODS, vanilla_rank)
    local_order = detail[order[:50]].argsort(descending=True, stable=True)
    detail_ranks = local_order.argsort() + 1
    base_ranks = torch.arange(1, 51)
    fused = 1.0 / (60.0 + base_ranks.double()) + 1.0 / (60.0 + detail_ranks.double())
    fused_ranks = fused.argsort(descending=True, stable=True).argsort() + 1
    return dict(vanilla=vanilla_rank, latest=int(detail_ranks[vanilla_rank - 1]),
                fusion=int(fused_ranks[vanilla_rank - 1]))


def summarize(dialogues, turn_rows):
    by_dialogue = {d['qid']: [] for d in dialogues}
    for row in turn_rows:
        by_dialogue[row['qid']].append(row)
    output = {}
    for horizon in (1, 2, 5, 10):
        output[horizon] = {}
        for method in METHODS:
            hits = unknown = gained = lost = 0
            for d in dialogues:
                turns = by_dialogue[d['qid']][:horizon]
                initial_hit = d['ranks'][0] <= 10
                hit = initial_hit or any(t[method] <= 10 for t in turns)
                baseline_hit = min(d['ranks'][:horizon + 1]) <= 10
                hits += hit
                gained += hit and not baseline_hit
                lost += baseline_hit and not hit
                unknown += not hit and len(turns) < horizon
            output[horizon][method] = dict(observed_hits=hits, unknown=unknown,
                                           gained=gained, lost_on_observed=lost)
    return output


@torch.inference_mode()
def main():
    start = time.monotonic()
    torch.set_num_threads(4)
    with (HERE / 'latest_facts.csv').open() as f:
        facts = {(r['question'], r['answer']): r['fact'] for r in csv.DictReader(f)}
    positions = {name: i for i, name in enumerate(json.loads(
        (INPUTS / 'Protocol/Search_Space_val_50k.json').read_text()))}
    dialogues = [json.loads(p.read_text()) for folder in PATHS['vanilla']
                 for p in sorted((ROOT / folder / 'dialogues').glob('*.json'))]
    assert len({d['qid'] for d in dialogues}) == len(dialogues) == 500
    cases = []
    for d in dialogues:
        assert len(d['turns']) + 1 == len(d['ranks']) == len(d['queries'])
        for r, t in enumerate(d['turns'], 1):
            assert t['query_after'] == d['queries'][r] and r == t['round'] + 1
            fact = facts[t['question'], t['answer']]
            cases.append((d, r, t['query_after'], fact))
    score = cpu_retriever()
    rows = []
    for start_index in range(0, len(cases), 16):
        batch = cases[start_index:start_index + 16]
        texts = list(dict.fromkeys(s for _, _, q, f in batch for s in (q, f) if s))
        sims = dict(zip(texts, score(texts)))
        for d, r, q, fact in batch:
            ranks = within_pool_ranks(sims[q], sims.get(fact), positions[d['target']])
            rows.append(dict(qid=d['qid'], round=r, saved_rank=d['ranks'][r],
                             fact=fact, **ranks))
        if start_index % 160 == 0:
            print(f'Scored {len(rows)}/{len(cases)} turns', flush=True)
    errors = [row for row in rows if row['saved_rank'] != row['vanilla']]
    boundary_errors = [row for row in errors if any(
        (row['saved_rank'] <= k) != (row['vanilla'] <= k) for k in (10, 50))]
    if boundary_errors:
        raise RuntimeError(f'Baseline changed at a decision boundary: {boundary_errors}')
    result = dict(dialogues=len(dialogues), turns=len(rows), fallback=sum(not r['fact'] for r in rows),
                  baseline_rank_differences=len(errors), boundary_differences=len(boundary_errors),
                  rounds=summarize(dialogues, rows), seconds=round(time.monotonic() - start, 1))
    with (HERE / 'turn_ranks.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    (HERE / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
