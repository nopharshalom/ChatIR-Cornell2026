"""Plot cumulative Hits@10 for vanilla and three matched S3 selectors."""

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
    max_rounds = config['max_rounds']
    arms = {'vanilla': 'Vanilla PlugIR', 's3_fused_top10': 'Fused Top 10',
            's3_current_top10': 'Current Top 10', 's3_history_mmr': 'History MMR'}
    expected_ids = config['sample_ids']
    ranks_by_arm = {}
    for arm in arms:
        ranks_by_arm[arm] = {}
        method_dir = run_dir / arm
        method_config = json.loads((method_dir / 'config.json').read_text())
        if any(config[key] != method_config[key] for key in config if key != 'arms'):
            raise ValueError(f'{arm} uses different settings or source files.')
        dialogues = method_dir / 'dialogues'
        if not dialogues.is_dir():
            dialogues = method_dir / arm / 'dialogues'
        for path in dialogues.glob('*.json'):
            record = json.loads(path.read_text())
            ranks = record['ranks']
            valid_stop = len(ranks) == max_rounds + 1 or ranks[-1] <= 10
            if not record.get('hits_only') or not valid_stop or any(r <= 10 for r in ranks[:-1]):
                raise ValueError(f'Invalid first-hit checkpoint in {path}')
            ranks_by_arm[arm][record['qid']] = ranks

    matched = [qid for qid in expected_ids if all(qid in ranks_by_arm[arm] for arm in arms)]
    if not matched or (not allow_partial and len(matched) != len(expected_ids)):
        raise ValueError(f'{len(matched)}/{len(expected_ids)} matched dialogues complete')
    if any(len({ranks_by_arm[arm][qid][0] for arm in arms}) != 1 for qid in matched):
        raise ValueError('Initial ranks differ for a matched dialogue')

    figures = run_dir / ('figures' if len(matched) == len(expected_ids) else 'partial_figures')
    figures.mkdir(exist_ok=True)
    rounds = range(max_rounds + 1)
    fig, ax = plt.subplots(figsize=(8, 4.5), layout='constrained')
    with (figures / 'hits_at_10.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['method', 'round', 'matched_dialogues', 'cumulative_hits', 'hits_at_10_pct'])
        for (arm, name), style, marker in zip(arms.items(), ('-', '--', ':', '-.'), ('o', 'x', 's', '^')):
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
