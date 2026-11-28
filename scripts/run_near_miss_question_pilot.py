"""Evaluate answer-conditioned near-miss promotion on the frozen 40 initial misses."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

import run_question_diversity as base
from guard_history_veto import guard_metal_timeout
from run_a2r_unseen_pilots import paired_summary
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, preflight, shared

OUT = ROOT / 'output/mmr-near-miss-question-pilot'
ARM = 'near_miss_probe'
SYSTEM = """Use the retrieved captions as evidence about possible images. Imagine two distinct,
plausible answers to the question that the evidence supports. For each, rewrite the current
description as a compact image retrieval caption assuming that answer. Preserve the existing
facts and add only the fact supplied by that answer. Do not identify a correct answer.
If two distinct answers are unsupported, return []. Return only JSON, in the format
[{"answer":"...", "caption":"..."},{"answer":"...", "caption":"..."}]."""


def answer_branches(pipe, query, evidence, question):
    prompt = json.dumps(dict(current_description=query, retrieved_captions=evidence, question=question))
    response = pipe.client.complete([dict(role='system', content=SYSTEM),
                                     dict(role='user', content=prompt)], 200, temperature=0)
    body = response.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:
        branches = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(branches, list) or len(branches) != 2:
        return []
    for field in ('answer', 'caption'):
        if any(not isinstance(b, dict) or not isinstance(b.get(field), str) for b in branches):
            return []
        values = {base.key(b[field]) for b in branches}
        if len(values) != 2 or '' in values or (field == 'caption' and base.key(query) in values):
            return []
    return branches


def decide(pipe, query, scores, evidence, questions, kl):
    if len(questions) == 1:
        return kl, dict(reason='single candidate', kl=kl, chosen=kl, candidates=[])
    order = scores.argsort().flip(0)[:50].tolist()
    near, plausible = set(order[10:]), set(order)
    records = []
    for q in questions:
        branches = answer_branches(pipe, query, evidence, q)
        score = None
        if branches:
            vectors = normalize(pipe.get_text_features([b['caption'] for b in branches]), dim=-1)
            top = (vectors @ pipe.img_embs.T).argsort(dim=1, descending=True)[:, :10].tolist()
            left, right = map(set, top)
            # Equal branch weights. Twice the score avoids floating-point ties.
            score = len((left ^ right) & near) - len(left - plausible) - len(right - plausible)
            for b, ids in zip(branches, top, strict=True):
                b.update(top10=ids, promoted=sorted(set(ids) & near))
        records.append(dict(question=q, branches=branches, score=score))
    best = max((r['score'] for r in records if r['score'] is not None), default=0)
    leaders = [r['question'] for r in records if r['score'] == best]
    chosen = leaders[0] if best > 0 and len(leaders) == 1 else kl
    reason = 'unique positive score' if best > 0 and len(leaders) == 1 else 'weak or tied'
    return chosen, dict(reason=reason, kl=kl, chosen=chosen, candidates=records)


def screen(pipe, refs, splits):
    """Compare frozen choices on first and later states from both groups, without tuning."""
    path = OUT/'screen.json'
    if path.exists():
        return
    states = []
    for split in splits:
        for i in split[:3]:
            turns = refs[i]['turns']
            for j in sorted({0, min(2, len(turns)-1)}):
                t = turns[j]
                q = t['query_before']
                scores = (normalize(pipe.get_text_features(q), dim=-1) @ pipe.img_embs.T).flatten()
                questions = t['candidate_questions']
                kl = pipe.select_question(q, questions, scores, 10, pipe.img_embs, 500, j+1)
                if kl != t['question']:
                    raise ValueError(f'Saved KL choice differs at {i}, round {j+1}')
                chosen, audit = decide(pipe, q, scores, t['selected_captions'], questions, kl)
                states.append(dict(qid=i, round=j+1, **audit))
                print(f'Screen: dialogue {i}, round {j+1}, changed={chosen != kl}', flush=True)
    shared.atomic_json(path, dict(n=len(states), changed=sum(s['kl']!=s['chosen'] for s in states),
                                  states=states))


def simulate(pipe, task, inputs, position):
    audit, evidence = [], []
    original_kl = pipe.select_question

    def generate(model, dialogue, query, captions):
        evidence[:] = captions
        return base.proposals(model, dialogue, query, captions, unique=True, audit=audit)

    def select(**kw):
        kl = original_kl(**kw)
        chosen, details = decide(pipe, kw['caption_recon'], kw['cossim_prev'], evidence,
                                 kw['questions'], kl)
        audit[-1].update(probe=details, changed=chosen != kl)
        return chosen

    shared.ROOT = inputs
    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(shared, 'question_candidates', generate), patch.object(pipe, 'select_question', select):
        row = shared.run_dialogue(pipe, 's3_current_mmr', task, position, hits_only=True)
    return dict(row, arm=ARM, proposal_audit=audit)


def validate(row, ref):
    if (row['qid'], row['queries'][0], row['target'], row['ranks'][0]) != (
            ref['qid'], ref['queries'][0], ref['target'], ref['ranks'][0]):
        raise ValueError('Unmatched control')
    same_prefix = True
    for j, (t, a) in enumerate(zip(row['turns'], row['proposal_audit'], strict=True)):
        questions = t['candidate_questions']
        if (not 1 <= len(questions) <= 5 or len(set(map(base.key, questions))) != len(questions)
                or a['probe']['chosen'] != t['question'] or t['question'] not in questions):
            raise ValueError('Invalid question slate or selection')
        if same_prefix:
            old = ref['turns'][j]
            if questions != old['candidate_questions'] or a['probe']['kl'] != old['question']:
                raise ValueError(f"Unmatched question generation before intervention for {row['qid']} R{j+1}")
            same_prefix = not a['changed']
    base.check_prefix(row, ref, 'changed')


@torch.inference_mode()
def run(check_only):
    cohorts, _, _ = preflight()
    tasks = {t['qid']: (t, folder) for _, group, _, folder in cohorts for t in group}
    splits = shared.read_json(base.OUT/'plan.json')['splits']
    ids = [i for split in splits for i in split]
    refs = shared.read_records(base.OUT, 'retry_fix_unique')
    if len(set(ids)) != 40 or list(map(len, splits)) != [20, 20] or refs.keys() != set(ids):
        raise ValueError('Expected the completed matched 40-dialogue control')
    if not all(refs[i]['finished_utc'] and refs[i]['ranks'][0] > 10 for i in ids):
        raise ValueError('Incomplete or ineligible control')
    files = [Path(__file__), Path(base.__file__), Path(mac.__file__), Path(shared.__file__),
             mac.HERE/'generate_dialog.py', GRID_WORKER]
    plan = dict(splits=splits, model=SNAPSHOT.name, control='retry_fix_unique', arm=ARM,
                mmr=.85, corpus=50000, max_rounds=10, near_miss_ranks=[11,50], branches=2,
                rule='unique positive maximum of branch-exclusive near-miss entrants minus total outside-50 entrants; else KL',
                primary='paired cumulative R10 Hits@10', secondary='paired cumulative R1, R3, R5; two splits',
                screen='first three IDs of each split, first and third active round (last if earlier)',
                controls_sha256=hashlib.sha256(json.dumps(refs,sort_keys=True).encode()).hexdigest(),
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    OUT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as session:
        lock = session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path) != plan:
            raise ValueError('Pilot plan differs from saved checkpoints')
        shared.atomic_json(path, plan)
        rows = shared.read_records(OUT, ARM)
        if not rows.keys() <= refs.keys():
            raise ValueError('Unexpected pilot IDs')
        for i, row in rows.items():
            validate(row, refs[i])
        print(f'Validated 40 matched controls and {len(rows)} probe checkpoints.', flush=True)
        if check_only:
            return
        if len(rows) < 40:
            with patch.object(mac, '__file__', str(GRID_WORKER)):
                worker = session.enter_context(closing(mac.MacQwen(SNAPSHOT, OUT)))
            pipe = mac.load_blip(INPUTS, worker)
            positions = {name:j for j,name in enumerate(pipe.search_space)}
            session.enter_context(patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)))
            screen(pipe, refs, splits)
            for i in ids:
                if i in rows:
                    continue
                task, inputs = tasks[i]
                print(f'Starting dialogue {i}', flush=True)
                row = simulate(pipe, task, inputs, positions[task['img']])
                validate(row, refs[i])
                shared.atomic_json(OUT/ARM/'dialogues'/f'{i}.json', row)
                rows[i] = row
                print(f'{len(rows)}/40 saved', flush=True)
        summary = dict(all=paired_summary(rows, refs),
                       groups=[paired_summary({i:rows[i] for i in split}, refs) for split in splits])
        shared.atomic_json(OUT/'summary.json', summary)
        print('Complete. Control and probe:', summary['all']['control'], summary['all']['variant'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
