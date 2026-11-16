"""Plot cumulative Hits@10 for the matched S2 and S3 variants."""

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
    arms = {'vanilla': 'Vanilla PlugIR', 's2_history_pool': 'S2 · All-history pool',
            's2_original_current_pool': 'S2 · Original + current pool',
            's3_fused_top10': 'S3 · Fused Top 10', 's3_current_top10': 'S3 · Current Top 10',
            's3_history_mmr': 'S3 · History MMR', 's3_current_mmr': 'S3 · Current MMR',
            's3_split_5_5': 'S3 · Split 5+5'}
    new_dir = run_dir / 'new_runs'
    new_config = json.loads((new_dir / 'config.json').read_text())
    new_arms = set(new_config['arms'])
    expected_ids = config['sample_ids']
    ranks_by_arm = {}
    for arm in arms:
        ranks_by_arm[arm] = {}
        is_new = arm in new_arms
        method_config = new_config if is_new else json.loads((run_dir / arm / 'config.json').read_text())
        ignored = {'arms', 'source_sha256'} if is_new else {'arms'}
        if any(config[key] != method_config[key] for key in config if key not in ignored):
            raise ValueError(f'{arm} uses different settings or source files.')
        dialogues = (new_dir if is_new else run_dir) / arm / 'dialogues'
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
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True, layout='constrained')
    with (figures / 'hits_at_10.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['method', 'round', 'matched_dialogues', 'cumulative_hits', 'hits_at_10_pct'])
        for arm, name in arms.items():
            first_hits = [next((r for r, rank in enumerate(ranks_by_arm[arm][qid]) if rank <= 10),
                               max_rounds + 1) for qid in matched]
            counts = [sum(hit <= r for hit in first_hits) for r in rounds]
            percentages = [100 * count / len(matched) for count in counts]
            writer.writerows((arm, r, len(matched), counts[r], percentages[r]) for r in rounds)
            for prefix, ax in zip(('s2_', 's3_'), axes):
                if arm == 'vanilla' or arm.startswith(prefix):
                    style = {'color': 'black', 'linestyle': '--'} if arm == 'vanilla' else {}
                    ax.step(rounds, percentages, where='post', label=name, marker='o', ms=3, **style)
    for title, ax in zip(('S2 · Candidate pools', 'S3 · Caption selectors'), axes):
        ax.set(title=title, xlabel='Round (0 = initial search)', xticks=list(rounds), ylim=(0, 104))
        ax.yaxis.set_major_formatter(PercentFormatter(100))
        ax.grid(axis='y', alpha=.25)
        ax.legend(loc='lower right')
    axes[0].set_ylabel('Targets found in top 10')
    fig.suptitle(f'Cumulative Hits@10 · {len(matched)}/{len(expected_ids)} matched dialogues')
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
