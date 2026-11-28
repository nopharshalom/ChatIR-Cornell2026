"""Paired MMR question-generation pilot on the two frozen groups of 20 dialogues."""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
from copy import deepcopy
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

import torch

from guard_history_veto import guard_metal_timeout
from run_a2r_unseen_pilots import paired_summary
from run_history_veto import classify
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, preflight, shared


OUT = ROOT / 'output/mmr-within-round-questions'
SPLIT_FILE = ROOT / 'output/mmr-history-veto/plan.json'
ARMS = ('retry_fix', 'retry_fix_unique', 'retry_fix_unique_history_exact')
GENERIC = 'what is the other object in the image?'
RETRY_HINT = ('[Drafting instruction]\nYour last draft repeated a proposal for this round. '
              'Ask about a different visible detail in the target photo.\n')
HEADER = re.compile(r'^(?:\s*[*#_]*\s*question\s*[*_]*\s*:\s*[*_]*\s*)+', re.I)


def key(text):
    text = HEADER.sub('', text).casefold().replace('’', "'").replace("'", '')
    return ' '.join(re.findall(r'[^\W_]+', text))


def question_text(response):
    return response['choices'][0]['message']['content'].split('?')[0] + '?'


def proposals(pipe, dialogue, query, captions, *, unique, audit):
    prompt = ''.join(f'{i}. {caption}\n' for i, caption in enumerate(captions))
    accepted, prior, seen = [], [], set()
    drafts = fallbacks = duplicates = 0
    retry_changed = False
    for _ in range(5):
        known = 0
        repeat = False
        for _ in range(3):
            response = pipe.generate_questions_referring(
                descrip=dialogue, prompt_fake=prompt + (RETRY_HINT if repeat else ''),
                n=1, ques_prior=accepted + prior)
            drafts += 1
            q = question_text(response)
            if 'uncertain' not in pipe.filter_questions(query, q):
                known += 1
                prior.append(q)
            elif unique and key(q) in seen:
                duplicates += 1
                repeat = True
            else:
                break
        else:
            q = None
        # The archived runner used len(prior) == 3 for this decision.
        retry_changed |= (known == 3) != (len(prior) == 3)
        if known == 3:
            fallbacks += 1
            q = question_text(pipe.generate_questions(dialogue, n=1))
            if 'uncertain' not in pipe.filter_questions(query, q):
                q = GENERIC
            if unique and key(q) in seen:
                duplicates += 1
                continue
        elif q is None:
            # At least one retry was spent on a duplicate. Stop at the slot budget.
            continue
        accepted.append(q)
        seen.add(key(q))
    audit.append(dict(drafts=drafts, fallbacks=fallbacks, duplicates=duplicates,
                      retry_changed=retry_changed))
    return accepted


def check_prefix(row, control, flag):
    """Both seeded trajectories must reproduce their control until a policy intervenes."""
    for j, (turn, audit) in enumerate(zip(row['turns'], row['proposal_audit'], strict=True)):
        old = control['turns'][j]
        for field in ('query_before', 'pool_ids', 'selected_ids'):
            if turn[field] != old[field]:
                raise ValueError(f"Dialogue {row['qid']}, round {j+1}: {field} differs before intervention")
        if flag == 'history_changed' and (turn['candidate_questions'] != old['candidate_questions']
                                         or audit['incumbent'] != old['question']):
            raise ValueError(f"Dialogue {row['qid']}, round {j+1}: proposals or original KL choice differ")
        if audit[flag]:
            return
        for field in ('candidate_questions', 'question', 'answer', 'query_after'):
            if turn[field] != old[field]:
                raise ValueError(f"Dialogue {row['qid']}, round {j+1}: unexpected difference in {field}")
        if row['ranks'][j+1] != control['ranks'][j+1]:
            raise ValueError(f"Dialogue {row['qid']}, round {j+1}: unexpected rank difference")
    if row['ranks'] != control['ranks']:
        raise ValueError(f"Dialogue {row['qid']}: unexpected stopping round")


def validate(row, ref, control, arm):
    if (row['target'], row['queries'][0], row['ranks'][0]) != (
            ref['target'], ref['queries'][0], ref['ranks'][0]):
        raise ValueError(f"Unmatched dialogue {row['qid']}")
    unique = arm != ARMS[0]
    for turn in row['turns']:
        qs = turn['candidate_questions']
        if not 1 <= len(qs) <= 5 or turn['question'] not in qs:
            raise ValueError('Invalid question slate or selected question')
        if (unique and len(set(map(key, qs))) != len(qs)) or (not unique and len(qs) != 5):
            raise ValueError(f'Question slate violates {arm}')
    flag = {ARMS[0]: 'retry_changed', ARMS[1]: 'duplicates', ARMS[2]: 'history_changed'}[arm]
    check_prefix(row, control, flag)


def history_options(dialogue, questions, incumbent):
    blocked = classify(dialogue, questions)
    kept = [q for i, q in enumerate(questions) if i not in blocked]
    eligible = kept or questions   # Same all-vetoed fallback as the completed exact-filter pilot.
    return eligible, dict(incumbent=incumbent, history_blocked=list(blocked),
                          history_all_blocked=not kept, history_changed=incumbent not in eligible,
                          eligible=len(eligible))


def reuse_if_identical(control):
    """Blocking only unselected questions leaves the whole seeded trajectory identical."""
    dialogue = [control['queries'][0]]
    audits = []
    for t, audit in zip(control['turns'], control['proposal_audit'], strict=True):
        _, extra = history_options(dialogue, t['candidate_questions'], t['question'])
        if extra['history_changed']:
            return None
        audits.append({**audit, **extra})
        dialogue.append(t['question'] + ' ' + t['answer'])
    row = deepcopy(control)
    row.update(arm=ARMS[2], proposal_audit=audits, execution='identical decisions from paired arm')
    return row


def simulate(pipe, task, inputs, position, arm):
    audit, history = [], []
    original_kl = pipe.select_question

    def generate(model, dialogue, query, captions):
        history[:] = dialogue
        return proposals(model, dialogue, query, captions, unique=arm != ARMS[0], audit=audit)

    def select(**kw):
        incumbent = original_kl(**kw)
        eligible, extra = history_options(history, kw['questions'], incumbent)
        audit[-1].update(extra)
        if extra['history_changed']:
            return original_kl(**{**kw, 'questions': eligible})
        return incumbent

    shared.ROOT = inputs
    pipe.client.next_seed = 1021 + task['qid']
    with ExitStack() as settings:
        settings.enter_context(patch.object(shared, 'question_candidates', generate))
        if arm == ARMS[2]:
            settings.enter_context(patch.object(pipe, 'select_question', select))
        row = shared.run_dialogue(pipe, 's3_current_mmr', task, position, hits_only=True)
    return dict(row, arm=arm, proposal_audit=audit, execution='live')


def diversity(rows):
    turns = [t for r in rows.values() for t in r['turns']]
    distinct = [len(set(map(key, t['candidate_questions']))) for t in turns]
    eligible = sum(a.get('eligible', len(t['candidate_questions'])) for r in rows.values()
                   for t, a in zip(r['turns'], r.get('proposal_audit', [{}] * len(r['turns'])), strict=True))
    return dict(turns=len(turns), proposals=sum(len(t['candidate_questions']) for t in turns),
                eligible_for_selection_per_turn=eligible / len(turns),
                distinct_per_turn=sum(distinct) / len(turns), distinct_histogram=dict(Counter(distinct)),
                round_one_distinct=sum(len(set(map(key, r['turns'][0]['candidate_questions'])))
                                       for r in rows.values()) / len(rows),
                llm_calls=sum(t['llm_calls'] for t in turns))


def report(ids, refs, rows):
    matched = {name: {i: data[i] for i in ids} for name, data in
               [('mmr85', refs), *rows.items()]}
    return dict(n=len(ids), diversity={name: diversity(data) for name, data in matched.items()},
                retry_vs_mmr=paired_summary(matched[ARMS[0]], matched['mmr85']),
                unique_vs_mmr=paired_summary(matched[ARMS[1]], matched['mmr85']),
                unique_vs_retry=paired_summary(matched[ARMS[1]], matched[ARMS[0]]),
                stacked_vs_unique=paired_summary(matched[ARMS[2]], matched[ARMS[1]]),
                stacked_vs_mmr=paired_summary(matched[ARMS[2]], matched['mmr85']))


@torch.inference_mode()
def main(check_only):
    groups, _, refs = preflight()
    splits = shared.read_json(SPLIT_FILE)['splits']
    ids = [i for group in splits for i in group]
    if [len(group) for group in splits] != [20, 20] or len(set(ids)) != 40 or any(
            refs[i]['ranks'][0] <= 10 for i in ids):
        raise ValueError('Expected the same two groups of 20 initial misses')
    tasks = {t['qid']: (t, root) for _, group, _, root in groups for t in group}
    sources = [Path(__file__), Path(__file__).with_name('run_history_veto.py'), Path(shared.__file__),
               mac.HERE/'generate_dialog.py', Path(mac.__file__), GRID_WORKER]
    plan = dict(splits=splits, arms=list(ARMS), model=SNAPSHOT.name, mmr=.85, max_rounds=10,
                corpus=50000, slots=5, attempts_per_slot=3, selector='original minimum KL',
                primary='paired cumulative Hits@10 at R10; earlier first hits secondary',
                controls=hashlib.sha256(json.dumps([refs[i] for i in ids], sort_keys=True).encode()).hexdigest(),
                sources={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    OUT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as session:
        lock = session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path) != plan:
            raise ValueError('Existing checkpoints belong to a different pilot configuration')
        shared.atomic_json(path, plan)
        rows = {arm: shared.read_records(OUT, arm) for arm in ARMS}
        controls = dict(zip(ARMS, (refs, rows[ARMS[0]], rows[ARMS[1]])))
        for arm, data in rows.items():
            control = controls[arm]
            if not data.keys() <= set(ids) & control.keys():
                raise ValueError(f'Unmatched saved IDs for {arm}')
            for i, row in data.items():
                validate(row, refs[i], control[i], arm)
        print('Ready: three variants, same 40 dialogues, two groups of 20, ten rounds, original KL.', flush=True)
        if check_only:
            return
        pipe = None
        session.enter_context(patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)))
        for i in ids:
            for arm in ARMS:
                if i in rows[arm]:
                    continue
                control = controls[arm][i]
                row = reuse_if_identical(control) if arm == ARMS[2] else None
                if row is None and pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Run the launcher in macOS Terminal for Metal access.')
                    with patch.object(mac, '__file__', str(GRID_WORKER)):
                        worker = session.enter_context(closing(mac.MacQwen(SNAPSHOT, OUT)))
                    pipe = mac.load_blip(INPUTS, worker)
                    positions = {name: k for k, name in enumerate(pipe.search_space)}
                if row is None:
                    task, inputs = tasks[i]
                    print(f'{arm}: starting dialogue {i}', flush=True)
                    row = simulate(pipe, task, inputs, positions[task['img']], arm)
                validate(row, refs[i], control, arm)
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
                rows[arm][i] = row
                print(f'{arm}: {len(rows[arm])}/40 saved', flush=True)
        summary = dict(all=report(ids, refs, rows), groups=[report(group, refs, rows) for group in splits])
        shared.atomic_json(OUT/'summary.json', summary)
        for name in ('retry_vs_mmr', 'unique_vs_mmr', 'unique_vs_retry', 'stacked_vs_unique', 'stacked_vs_mmr'):
            pair = summary['all'][name]
            print(f"{name}: R10 {pair['variant'][-1]}/40 vs {pair['control'][-1]}/40", flush=True)
        for name, stats in summary['all']['diversity'].items():
            print(f"{name}: {stats['distinct_per_turn']:.2f} distinct proposals per turn", flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        main(parser.parse_args().check_only)
