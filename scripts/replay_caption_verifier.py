"""First-question paired verification, with blind caption judgments and a Qwen runner."""
import argparse
from collections import Counter
import json
from pathlib import Path
import random

import torch

from analyze_500_dialogues import PATHS
from run_probe_pool_sweep import INPUTS, ROOT, SNAPSHOT, cpu_retriever, mac, shared

OUT = ROOT / 'output/agentir-caption-verifier-r1'
RULE = """Use only the latest question, the user's answer and each image caption. Judge only the detail supplied by the answer. Treat each caption independently, even when captions repeat.
CONTRADICT means the caption explicitly states an incompatible value for the same person, object and attribute. Missing detail, a different object, a different person, an ambiguous referent, and a related scene are UNKNOWN.
MATCH means the caption explicitly confirms the answer for the relevant subject. For 'what other object?', the caption must mention the answered object or an unambiguous synonym. Do not infer unstated facts. If the user is unsure, abstain.
Example: Q 'Is the boy's jacket red?' A 'Yes'. 'The boy has a blue jacket' contradicts. 'Boy in a red jacket' matches. 'A boy in a park', 'a red bicycle' and 'a girl in a blue jacket' are unknown.
A caption can mention other people or objects without excluding the one asked about. Different sports, locations, or activities may coexist; call a contradiction only when the caption clearly rules out the actual answer."""


@torch.inference_mode()
def prepare():
    torch.set_num_threads(4)
    OUT.mkdir(parents=True, exist_ok=True)
    dialogs = [json.loads(p.read_text()) for folder in PATHS['vanilla']
               for p in sorted((ROOT / folder / 'dialogues').glob('*.json'))]
    cases = [d for d in dialogs if d['ranks'][0] > 10]
    assert len(dialogs) == len({d['qid'] for d in dialogs}) == 500 and len(cases) == 136
    random.Random(4175).shuffle(cases)
    corpus = shared.read_json(INPUTS / 'Protocol/visdial_captions.json')
    names = shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json')
    assert [i['id'] for i in corpus] == names
    score = cpu_retriever()
    blind, key = [], []
    for start in range(0, len(cases), 16):
        batch = cases[start:start+16]
        scores = score([d['queries'][1] for d in batch])
        for d, s in zip(batch, scores):
            ids = s.argsort(descending=True, stable=True)[:50].tolist()
            targets = [j for j, i in enumerate(ids, 1) if names[i] == d['target']]
            pos = targets[0] if targets else None
            assert pos == (d['ranks'][1] if d['ranks'][1] <= 50 else None)
            q = d['turns'][0]
            case = len(blind) + 1
            blind.append(dict(case=case, question=q['question'], answer=q['answer'],
                              captions=[corpus[i]['caption'][0] for i in ids]))
            key.append(dict(case=case, qid=d['qid'], position=pos))
    shared.atomic_json(OUT / 'blind.json', blind)
    shared.atomic_json(OUT / 'key.json', key)


def prompt(case, label):
    first, last = (1, 10) if label == 'CONTRADICT' else (11, 50)
    options = '\n'.join(f'{i}. {case["captions"][i - 1]}' for i in range(first, last + 1))
    return [dict(role='system', content=RULE),
            dict(role='user', content=f"Q: {case['question']}\nA: {case['answer']}\n{options}\n"
                 f"Return just a JSON array of the IDs that {label}. Return [] if none.")]


def check(ids, label):
    first, last = (1, 10) if label == 'CONTRADICT' else (11, 50)
    if (not isinstance(ids, list) or any(type(i) is not int or not first <= i <= last for i in ids)
            or len(ids) != len(set(ids))):
        raise ValueError(f'Invalid {label} IDs: {ids}')
    return sorted(ids)


def ask(worker, case, label):
    messages = prompt(case, label)
    for _ in range(2):
        raw = worker.complete(messages, 128)
        try:
            return check(json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()), label)
        except (ValueError, TypeError):
            messages.append(dict(role='assistant', content=raw))
            messages.append(dict(role='user', content='Return only the JSON array of valid caption IDs.'))
    raise RuntimeError(f'Could not parse case {case["case"]}, {label}: {raw}')


def qwen(cases):
    path = OUT / 'qwen_judgments.json'
    results = shared.read_json(path) if path.exists() else {}
    worker = mac.MacQwen(SNAPSHOT, OUT)
    try:
        # Check attribute binding, missing details and an uncertain user before scoring.
        samples = ['a boy playing in a park', 'the boy is wearing a blue jacket',
                   'the boy is wearing a red jacket', 'a girl in a blue jacket',
                   'a boy riding a red bicycle']
        control = dict(question="Is the boy's jacket red?", answer='yes',
                       captions=samples + ['a park'] * 5 + samples + ['a park'] * 35, case='control')
        controls = []
        for answer, label, expected in [('yes', 'CONTRADICT', [2]), ('yes', 'MATCH', [13]),
                                        ('unsure', 'CONTRADICT', [])]:
            actual = ask(worker, {**control, 'answer': answer}, label)
            controls.append(dict(answer=answer, label=label, expected=expected, actual=actual))
        shared.atomic_json(OUT / 'qwen_controls.json', controls)
        print(f"Prompt controls passed: {sum(x['actual'] == x['expected'] for x in controls)}/3", flush=True)
        for c in cases:
            key = str(c['case'])
            if key in results:
                continue
            bad = ask(worker, c, 'CONTRADICT')
            good = ask(worker, c, 'MATCH') if bad else []
            results[key] = dict(bad=bad, good=good)
            shared.atomic_json(path, results)
            if len(results) % 20 == 0:
                print(f'Verified {len(results)}/{len(cases)}', flush=True)
    finally:
        worker.close()
    return results


def evaluate(key, labels):
    assert {str(c['case']) for c in key} == set(labels)
    totals = Counter()
    for c in key:
        val = labels[str(c['case'])]
        bad, good = check(val['bad'], 'CONTRADICT'), check(val['good'], 'MATCH')
        # If replacements are scarce, remove the worst vanilla-ranked contradictions first.
        n = min(len(bad), len(good))
        displayed = (set(range(1, 11)) - set(sorted(bad, reverse=True)[:n])) | set(good[:n])
        assert len(displayed) == 10
        base = c['position'] in range(1, 11)
        hit = c['position'] in displayed
        totals.update(dict(cases=1, flagged_cases=bool(bad), swap_cases=n>0, swaps=n,
                           vanilla=base, verified=hit, gained=hit and not base, lost=base and not hit))
    return dict(totals)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--labels', type=Path)
    args = parser.parse_args()
    if not (OUT/'key.json').exists() or not (OUT/'blind.json').exists():
        prepare()
    if not args.prepare_only:
        labels = shared.read_json(args.labels) if args.labels else qwen(shared.read_json(OUT/'blind.json'))
        print(json.dumps(evaluate(shared.read_json(OUT/'key.json'), labels), indent=2))
