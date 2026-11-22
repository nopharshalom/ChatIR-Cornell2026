"""Reproduce the completed 200-dialogue novelty/grounding audit; no model calls."""
import collections
import csv
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent
OUT = REPO / 'analysis/novelty_grounding200'
SOURCE = ROOT / 'output/agentir-novelty-grounding-200'
PATHS = {
    'vanilla': ROOT / 'output/plugir-overnight-hits10-200/results/vanilla/dialogues',
    'novelty': SOURCE / 'novelty/dialogues',
    'grounded_rewrite': SOURCE / 'grounded_rewrite/dialogues',
}


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    data, sources = {}, []
    for arm, path in PATHS.items():
        data[arm] = {}
        for f in sorted(path.glob('*.json')):
            raw = f.read_bytes()
            d = json.loads(raw)
            assert d['qid'] not in data[arm]
            assert len(d['ranks']) == len(d['turns']) + 1
            data[arm][d['qid']] = d
            sources.append(dict(path=str(f.relative_to(ROOT)), sha256=hashlib.sha256(raw).hexdigest()))
    baseline = data['vanilla']
    assert len(baseline) == 200
    for ds in data.values():
        assert set(ds) == set(baseline)
        assert all(d['ranks'][0] == baseline[q]['ranks'][0] for q, d in ds.items())
    result, rows, traces = {}, [], []
    for arm, ds in data.items():
        c = collections.Counter()
        changed = []
        repeated_cases = []
        for q, d in sorted(ds.items()):
            seen = set()
            triggered = False
            for k, t in enumerate(d['turns']):
                question = norm(t['question'])
                c['turns'] += 1
                c['repeated_questions'] += question in seen
                seen.add(question)
                c['generic_other_object_questions'] += question == 'what is the other object in the image'
                c['unchanged_queries'] += t['query_after'] == t['query_before']
                c['query_words'] += len(t['query_after'].split())
                c['llm_calls'] += t.get('llm_calls', 0)
                if k and k + 1 < len(d['turns']) and not triggered:
                    previous = d['turns'][k-1]
                    if (question, norm(t['answer'])) == (norm(previous['question']), norm(previous['answer'])):
                        triggered = True
                        repeated_cases.append(dict(qid=q, after_round=k+1, eventual_hit=min(d['ranks']) <= 10))
                if arm == 'novelty':
                    n = t['novelty']
                    c['fallback_turns'] += n['fallback']
                    c['regenerated_turns'] += bool(n['regenerations'])
                    c['invalid_initial_judgements'] += not n['judgements'][0]['valid']
                    c['judgements'] += len(n['judgements'])
                    c['invalid_judgements'] += sum(not j['valid'] for j in n['judgements'])
                elif arm == 'grounded_rewrite':
                    c['fallback_turns'] += t['grounding']['fallback']
            v = baseline[q]
            win, vwin = min(d['ranks']) <= 10, min(v['ranks']) <= 10
            if arm != 'vanilla' and win != vwin:
                first = next((k for k, (t, u) in enumerate(zip(d['turns'], v['turns']))
                              if t['question'] != u['question'] or t['query_after'] != u['query_after']), None)
                row = dict(arm=arm, qid=q, outcome='gain' if win else 'loss',
                           first_divergence_round=first+1 if first is not None else None,
                           invalid_judge_at_divergence=(not d['turns'][first]['novelty']['judgements'][0]['valid'])
                           if arm == 'novelty' and first is not None else None)
                changed.append(row)
                rows.append(row)
            # Preserve all active trajectories, including shared failures and successes.
            if d['turns']:
                traces += [f'\n## {arm}: {q}', f'Initial: {d["queries"][0]}', f'Ranks: {d["ranks"]}']
                for t in d['turns']:
                    traces += [f'\nRound {t["round"]+1}: {t["question"]}',
                               f'Answer: {t["answer"]}', f'Query: {t["query_after"]}']
                    if arm == 'novelty':
                        traces.append('Novelty decisions: ' + json.dumps(t['novelty'], ensure_ascii=False))
        rounds = {}
        for r in [0, 1, 2, 5, 10]:
            hit = {q for q, d in ds.items() if min(d['ranks'][:r+1]) <= 10}
            ref = {q for q, d in baseline.items() if min(d['ranks'][:r+1]) <= 10}
            rounds[r] = dict(hits=len(hit), percent=len(hit)/2, delta_pp=(len(hit)-len(ref))/2,
                             gained=sorted(hit-ref), lost=sorted(ref-hit))
        result[arm] = dict(completed=len(ds), rounds=rounds, diagnostics=dict(c),
                           minutes=sum(d['seconds'] for d in ds.values())/60,
                           repeated_answer_trigger_cases=repeated_cases, discordant=changed)
    (OUT/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
    (OUT/'sources.json').write_text(json.dumps(sources, indent=2)+'\n')
    (OUT/'traces.md').write_text('# Saved dialogue traces\n\nRanks are evaluation-only; no model reruns.\n'+'\n'.join(traces)+'\n')
    with (OUT/'discordant_pairs.csv').open('w') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({a: {'round10': s['rounds'][10], 'diagnostics': s['diagnostics']}
                      for a, s in result.items()}, indent=2))


if __name__ == '__main__':
    main()
