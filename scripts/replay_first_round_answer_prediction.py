"""Diagnose first-round answer predictions using saved outcomes only. No Qwen calls."""
import json
import re

import torch

from run_probe_pool_sweep import INPUTS, ROOT, cpu_retriever, shared

SCREEN = ROOT / 'output/mmr-first-round-question-screen'
CONTROLS = ROOT / 'output/mmr-within-round-questions/retry_fix_unique/dialogues'
OUT = ROOT / 'output/mmr-answer-prediction-replay'
MODES = ('original', 'real_rewrite_first', 'real_rewrite_second',
         'template_predicted', 'template_real_first', 'template_real_second')


def template(query, question, answer):
    clean_question = re.sub(r"^question:\s*", "", question, flags=re.I)
    return f"{query}. {clean_question} {answer}"


def norm(answer):
    return ' '.join(re.findall(r"\w+", answer.casefold()))


def score_pair(top_a, top_b, near):
    return len((set(top_a) ^ set(top_b)) & near)


def choose(scores, kl):
    high = max((s for s in scores.values() if s is not None), default=0)
    best = [q for q, s in scores.items() if s == high]
    return best[0] if high > 0 and len(best) == 1 else kl


@torch.inference_mode()
def main():
    ids = [i for split in shared.read_json(SCREEN/'plan.json')['splits'] for i in split]
    assert len(set(ids)) == len(ids) == 40
    corpus = shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json')
    positions = {path: i for i, path in enumerate(corpus)}
    states, texts = [], set()
    for i in ids:
        row = shared.read_json(SCREEN/'cases'/f'{i}.json')
        control = shared.read_json(CONTROLS/f'{i}.json')
        turn = control['turns'][0]
        query = turn['query_before']
        assert row['qid'] == control['qid'] == i
        assert list(row['outcomes']) == turn['candidate_questions']
        states.append((i, row, turn, positions[control['target']]))
        for c in row['selection']['candidates']:
            q, branches = c['question'], c['branches']
            outcome = row['outcomes'][q]
            texts.add(outcome['rewrite'])
            if branches:
                for answer in [outcome['answer'], *(b['answer'] for b in branches)]:
                    texts.add(template(query, q, answer))

    retrieve = cpu_retriever()
    texts = sorted(texts)
    tops = {}
    for start in range(0, len(texts), 16):
        batch = texts[start:start+16]
        for text, scores in zip(batch, retrieve(batch), strict=True):
            tops[text] = scores.argsort().flip(0)[:10].tolist()

    cases = []
    for i, row, turn, target in states:
        kl = row['selection']['kl']
        query = turn['query_before']
        near = set(turn['pool_ids'][10:50])
        candidates = {}
        for c in row['selection']['candidates']:
            q, branches = c['question'], c['branches']
            outcome = row['outcomes'][q]
            actual = tops[outcome['rewrite']]
            hit = outcome['rank'] <= 10
            assert (target in actual) == hit, (i, q, 'CPU replay differs at Hits@10')
            info = dict(rank=outcome['rank'], answer=outcome['answer'], predicted=[b['answer'] for b in branches],
                        scores=dict.fromkeys(MODES), before_target_hit=None, exact_answer=None)
            if branches:
                a, b = branches
                fa, fb, real = [tops[template(query, q, ans)] for ans in
                                (a['answer'], b['answer'], outcome['answer'])]
                pairs = [(a['top10'], b['top10']), (actual, b['top10']), (a['top10'], actual),
                         (fa, fb), (real, fb), (fa, real)]
                info['scores'] = {m:score_pair(*pair, near) for m,pair in zip(MODES,pairs,strict=True)}
                info['before_target_hit'] = any(target in branch['top10'] for branch in branches)
                info['exact_answer'] = norm(outcome['answer']) in [norm(x['answer']) for x in branches]
                info['template_predicted_hit'] = target in fa or target in fb
                info['template_real_hit'] = target in real
                assert info['scores']['original'] == c['score'], (i, q, 'saved score mismatch')
            candidates[q] = info
        choices = {m: choose({q:d['scores'][m] for q,d in candidates.items()}, kl) for m in MODES}
        assert choices['original'] == row['selection']['probe']
        cases.append(dict(qid=i, kl=kl, choices=choices, candidates=candidates))

    summary = {}
    for mode in MODES:
        rescue, loss, changed = [], [], []
        hits = 0
        for c in cases:
            q, kl = c['choices'][mode], c['kl']
            old, new = [c['candidates'][sel]['rank'] <= 10 for sel in (kl, q)]
            hits += new
            if q != kl:
                changed.append(c['qid'])
            if new and not old:
                rescue.append(c['qid'])
            if old and not new:
                loss.append(c['qid'])
        summary[mode] = dict(hits=hits, rescues=rescue, losses=loss, changed=len(changed))
    shared.atomic_json(OUT/'replay.json',dict(modes=list(MODES),summary=summary,cases=cases))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    torch.set_num_threads(4)
    main()
