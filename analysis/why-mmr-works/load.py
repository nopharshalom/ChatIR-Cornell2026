"""Load the matched 400-dialogue k-means vs MMR runs (samples A and B) from experiments/."""
import json, glob, os
from pathlib import Path
R = str(Path(__file__).resolve().parents[2])  # repo root
E = R + '/experiments'
SS = json.load(open(R + '/Protocol/Search_Space_val_50k.json'))
IDX = {s: i for i, s in enumerate(SS)}
CAPS = [c['caption'][0] for c in json.load(open(R + '/Protocol/visdial_captions.json'))]
A_IDS = [t['qid'] for t in json.load(open(E + '/2026-11-plugir/plugir-overnight-hits10-200/sample.json'))['tasks']]
B_IDS = [t['qid'] for t in json.load(open(E + '/2026-11-22-plugir/results/agentir-vanilla-all-round-probes-200/plan.json'))['tasks']]
ARMS = {
 'A': {
  'kmeans (vanilla)': '2026-11-plugir/plugir-overnight-hits10-200/results/vanilla/dialogues',
  'MMR λ=0.85': '2026-11-22-plugir/results/paper-original200-mmr85-and-caption/current_mmr85/dialogues',
  'MMR λ=0.7': '2026-11-plugir/plugir-overnight-hits10-200/results/new_runs/s3_current_mmr/dialogues',
  'top-10 (λ=1)': '2026-11-plugir/plugir-overnight-hits10-200/results/s3_current_top10/dialogues',
 },
 'B': {
  'kmeans (vanilla)': '2026-11-22-plugir/results/agentir-overnight-200-300/vanilla/dialogues',
  'MMR λ=0.85': '2026-11-22-plugir/results/agentir-mmr-history-visual-200/mmr85/dialogues',
  'MMR λ=0.7': '2026-11-22-plugir/results/agentir-overnight-200-300/s3_current_mmr/dialogues',
  'MMR λ=0.5': '2026-11-22-plugir/results/agentir-mmr-history-visual-200/mmr50/dialogues',
 }}
def load(sample, arm):
    d = E + '/' + ARMS[sample][arm]
    ids = A_IDS if sample == 'A' else B_IDS
    out = {}
    for q in ids:
        p = f'{d}/{q}.json'
        if os.path.exists(p):
            out[q] = json.load(open(p))
    return out
