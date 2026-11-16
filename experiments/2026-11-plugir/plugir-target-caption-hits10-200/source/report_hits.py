"""Compare vanilla PlugIR and guaranteed target-caption exposure."""

import argparse
import csv
import json
from pathlib import Path


def report(run_dir, allow_partial=False):
    import matplotlib

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    config = json.loads((run_dir / 'vanilla/config.json').read_text())
    new_dir = run_dir / 'new_runs'
    variant_config = json.loads((new_dir / 'config.json').read_text())
    if any(config[key] != variant_config[key] for key in config if key not in ('arms', 'source_sha256')):
        raise ValueError('Baseline and experiment settings differ.')
    max_rounds = config['max_rounds']
    arms = {'vanilla': 'Vanilla PlugIR', 'target_caption': 'Target caption guaranteed'}
    expected_ids = config['sample_ids']
    ranks_by_arm = {}
    exposure_by_arm = {}
    for arm in arms:
        ranks_by_arm[arm] = {}
        exposure_by_arm[arm] = {}
        base = run_dir if arm == 'vanilla' else new_dir
        for path in (base / arm / 'dialogues').glob('*.json'):
            record = json.loads(path.read_text())
            ranks = record['ranks']
            valid_stop = len(ranks) == max_rounds + 1 or ranks[-1] <= 10
            if not record.get('hits_only') or not valid_stop or any(r <= 10 for r in ranks[:-1]):
                raise ValueError(f'Invalid first-hit checkpoint in {path}')
            ranks_by_arm[arm][record['qid']] = ranks
            turns = record['turns']
            shown = [turn['round'] for turn in turns if turn['target_shown']]
            if arm == 'target_caption' and len(shown) != len(turns):
                raise ValueError(f'Missing target exposure in {path}')
            first_hit = next((i for i, rank in enumerate(ranks) if rank <= 10), None)
            exposure_by_arm[arm][record['qid']] = (
                ranks[0], first_hit, shown[0] if shown else None, len(shown),
                sum(t.get('injected_slot') is not None for t in turns),
                sum(t.get('injected_slot') is not None and not t['target_in_pool'] for t in turns),
                sum(t['target_shown'] and not t['target_in_pool'] for t in turns),
                (first_hit - shown[0]) if shown and first_hit is not None else None,
            )

    matched = [qid for qid in expected_ids if all(qid in ranks_by_arm[arm] for arm in arms)]
    if not matched or (not allow_partial and len(matched) != len(expected_ids)):
        raise ValueError(f'{len(matched)}/{len(expected_ids)} matched dialogues complete')
    if any(ranks_by_arm['vanilla'][qid][0] != ranks_by_arm['target_caption'][qid][0]
           for qid in matched):
        raise ValueError('Initial ranks differ for a matched dialogue')

    figures = run_dir / ('figures' if len(matched) == len(expected_ids) else 'partial_figures')
    figures.mkdir(exist_ok=True)
    rounds = range(max_rounds + 1)
    with (figures / 'target_exposure.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['method', 'qid', 'initial_rank', 'first_hit_round', 'first_exposure_round',
                         'exposure_count', 'injections', 'outside_pool_injections',
                         'outside_pool_exposures', 'questions_from_first_exposure_to_hit'])
        for arm in arms:
            writer.writerows((arm, qid, *exposure_by_arm[arm][qid]) for qid in matched)
    fig, ax = plt.subplots(figsize=(8, 4.5), layout='constrained')
    with (figures / 'hits_at_10.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['method', 'round', 'matched_dialogues', 'cumulative_hits', 'hits_at_10_pct'])
        for (arm, name), style, marker in zip(arms.items(), ('-', '--'), ('o', 'x')):
            first_hits = [next((r for r, rank in enumerate(ranks_by_arm[arm][qid]) if rank <= 10),
                               max_rounds + 1) for qid in matched]
            counts = [sum(hit <= r for hit in first_hits) for r in rounds]
            percentages = [100 * count / len(matched) for count in counts]
            writer.writerows((arm, r, len(matched), counts[r], percentages[r]) for r in rounds)
            ax.step(rounds, percentages, where='post', label=name, linestyle=style, marker=marker)
    ax.set(title=f'Cumulative Hits@10 · {len(matched)}/{len(expected_ids)} matched dialogues',
           xlabel='Round (0 = initial search)', ylabel='Targets found in top 10',
           xticks=list(rounds), ylim=(0, 104))
    ax.yaxis.set_major_formatter(PercentFormatter(100))
    ax.grid(axis='y', alpha=.25)
    ax.legend(loc='lower right')
    fig.savefig(figures / 'hits_at_10.png', dpi=180)
    plt.close(fig)
    (figures / 'matched_dialogue_ids.json').write_text(json.dumps(matched, indent=2) + '\n')
    return figures


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    print(report(args.run_dir, args.allow_partial))
