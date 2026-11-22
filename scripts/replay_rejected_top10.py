"""Replay saved vanilla dialogues, hiding previously rejected display results."""
import argparse
import json

import torch

from analyze_500_dialogues import PATHS, first_hit
from run_probe_pool_sweep import INPUTS, ROOT, cpu_retriever, shared

@torch.inference_mode()
def main(sample_size):
    folder = ROOT / PATHS['vanilla'][{200: 0, 300: 1}[sample_size]] / 'dialogues'
    images = json.loads((INPUTS / 'Protocol/Search_Space_val_50k.json').read_text())
    positions = {name: i for i, name in enumerate(images)}
    assert len(positions) == len(images) == 50000
    score = None
    torch.set_num_threads(4)
    rows = []
    for file in sorted(folder.glob('*.json')):
        data = json.loads(file.read_text())
        target = positions[data['target']]
        baseline = first_hit(data)
        rejected = set()
        hit = 11
        for r, rank in enumerate(data['ranks']):
            if rank <= 10:
                assert target not in rejected
                hit = r
                break
            # Even after every earlier rejection, the target cannot reach the screen.
            if r == 10 and rank > len(rejected) + 10:
                break
            order = data['turns'][r]['pool_ids'] if r < len(data['turns']) else []
            if target in order:
                assert order.index(target) + 1 == rank
            elif order:
                assert rank > len(order)
            screen = [i for i in order if i not in rejected][:10]
            if len(screen) < 10:
                if score is None:
                    score = cpu_retriever()
                scores = score([data['queries'][r]])[0]
                ranked = scores.argsort(descending=True).tolist()
                assert ranked.index(target) + 1 == rank, (data['qid'], r, rank)
                # Prefer exact recorded ordering where available.
                prefix = set(order)
                order = order + [i for i in ranked if i not in prefix]
                screen = [i for i in order if i not in rejected][:10]
            if target in screen:
                hit = r
                break
            assert len(set(screen)) == 10
            rejected.update(screen)
        assert hit <= baseline
        rows.append(dict(qid=data['qid'], vanilla=baseline, filtered=hit))
    assert len({row['qid'] for row in rows}) == len(rows) == sample_size
    hits = {r: [sum(row[arm] <= r for row in rows) for arm in ('vanilla', 'filtered')]
            for r in (0, 1, 2, 5, 10)}
    questions = {arm: sum(min(row[arm], 10) for row in rows) for arm in ('vanilla', 'filtered')}
    saved = dict(hits=hits, questions=questions, rows=rows)
    shared.atomic_json(ROOT / f'output/agentir-rejected-top10-{sample_size}/replay.json', saved)
    print(hits, questions)
    print('Earlier among vanilla hits:', sum(x['filtered'] < x['vanilla'] <= 10 for x in rows))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-size', type=int, choices=(200, 300), default=200)
    main(parser.parse_args().sample_size)
