"""Paired BLIP color check using saved first answers and the vanilla top-10 boundary."""
import csv
import json
import re

import torch

from analyze_500_dialogues import PATHS
from run_probe_pool_sweep import INPUTS, REPO, ROOT, cpu_retriever

# Fixed from the question and answer alone, before inspecting targets or scores.
ALTERNATIVE = {
    1:'black', 2:'green', 7:'gray', 15:'gold', 16:'blue', 20:'red',
    21:'white', 26:'blue', 29:'black', 30:'tan', 35:'white', 37:'yellow',
    38:'blue', 43:'white', 48:'white', 49:'blue', 50:'green', 51:'black',
    58:'white', 59:'red', 60:'white', 63:'black', 64:'green', 65:'tan',
    69:'red', 72:'red', 73:'black', 75:'black', 88:'black', 90:'red',
    91:'red', 92:'blue', 98:'gray', 105:'beige', 106:'brown', 107:'blue',
    108:'blue', 115:'gray', 116:'white', 123:'black', 124:'black', 130:'brown',
}


def replace_color(fact, answer, alternate):
    def replace(m):
        article = ('an ' if alternate[0] in 'aeiou' else 'a ') if m[1] else ''
        return article + alternate
    text, n = re.subn(r'\b(an? )?' + re.escape(answer) + r'\b', replace, fact)
    assert n == 1 and answer != alternate
    return text


@torch.inference_mode()
def run():
    torch.set_num_threads(4)
    pilot = ROOT / 'output/agentir-caption-verifier-r1'
    cases = [c for c in json.loads((pilot / 'blind.json').read_text()) if 'color' in c['question'].lower()]
    assert set(ALTERNATIVE) == {c['case'] for c in cases}
    key = {k['case']:k for k in json.loads((pilot / 'key.json').read_text())}
    with (REPO/'analysis/latest-answer-top50/latest_facts.csv').open() as fin:
        facts = {(v['question'],v['answer']):v['fact'] for v in csv.DictReader(fin)}
    dialogs = {}
    for folder in PATHS['vanilla']:
        for p in (ROOT / folder / 'dialogues').glob('*.json'):
            d = json.loads(p.read_text())
            dialogs[d['qid']] = d
    names = json.loads((INPUTS/'Protocol/Search_Space_val_50k.json').read_text())
    positions = {name:i for i,name in enumerate(names)}
    score = cpu_retriever()
    rows = []
    for c in cases:
        k = key[c['case']]
        d = dialogs[k['qid']]
        actual = facts[c['question'], c['answer']]
        foil = replace_color(actual, c['answer'].lower().strip('. '), ALTERNATIVE[c['case']])
        baseline, yes, other = score([d['queries'][1], actual, foil])
        order = baseline.argsort(descending=True, stable=True)
        target = positions[d['target']]
        rank = int((order == target).nonzero().item()) + 1
        assert rank == d['ranks'][1]
        tenth = int(order[9])
        gap = yes - other
        best_outside = int(order[10:50][gap[order[10:50]].argmax()])
        rows.append(dict(case=c['case'], qid=k['qid'], actual=actual, foil=foil, rank=rank,
                         target_favors_answer=bool(gap[target] > 0), target_margin=float(gap[target]),
                         tenth_margin=float(gap[tenth]), beats_tenth=bool(gap[target] > gap[tenth]),
                         raw_beats_tenth=bool(yes[target] > yes[tenth]), best_outside_is_target=target==best_outside))
    out = ROOT / 'output/agentir-first-answer-colors'
    out.mkdir(exist_ok=True)
    with (out / 'paired.csv').open('w',newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    near = [r for r in rows if 11 <= r['rank'] <= 50]
    for name, sample in [('all',rows),('near',near)]:
        print(name,len(sample),{k:sum(r[k] for r in sample) for k in
                                ('target_favors_answer','beats_tenth','raw_beats_tenth','best_outside_is_target')})
    for r in near:
        print(r['actual'], 'vs', r['foil'], r['rank'], round(r['target_margin'],4),round(r['tenth_margin'],4), r['best_outside_is_target'])


if __name__ == '__main__':
    run()
