"""Select a question by how often its simulated answer retrieves the answering image."""
import argparse
from contextlib import ExitStack, closing, contextmanager
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
import random
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

from guard_history_veto import guard_metal_timeout
from run_a2r_unseen_pilots import paired_summary
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, mac, preflight, shared

OUT = ROOT / 'output/proxy-image-retrieval'
WORKER = Path(__file__).with_name('proxy_sampling_worker.py')
ARMS = {'mmr85_proxy': 's3_current_mmr', 'vanilla_proxy': 'vanilla'}
SEED = 57871127


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sample_ranks(qid, round_number):
    # Draw one one-based rank per stratum; paired arms draw identical rank positions.
    rng = random.Random(f'{SEED}/{qid}/{round_number}')
    return [rng.randrange(lo, lo + 4) for lo in range(11, 51, 4)]


@contextmanager
def isolated_rng(client):
    def command(op):
        client.process.stdin.write(json.dumps({'proxy_rng': op}) + '\n')
        client.process.stdin.flush()
        if client.read() != {'proxy_rng': op}:
            raise RuntimeError('Unexpected worker response')

    pending_seed = client.next_seed
    command('save')
    try:
        yield
    finally:
        client.next_seed = pending_seed
        command('restore')


def score_question(pipe, history, query, question, proxies):
    # Data shared between arms only when all inputs to a simulation are identical.
    fingerprint = digest([history, query, question, proxies])
    path = OUT / 'branch-cache' / f'{fingerprint}.json'
    if path.exists():
        return shared.read_json(path)
    outcomes = []
    rewrites = {}
    for image_id in proxies:
        # A stable per-image seed makes a branch independent of question order and cache hits.
        pipe.client.next_seed = SEED + image_id
        answer = pipe.client.answer(REPO / 'data' / pipe.search_space[image_id], question)
        if answer not in rewrites:
            updated = pipe.reconstruct_dialog([*history, f'{question} {answer}'])
            if updated == query:
                pipe.client.next_seed = SEED
                updated = pipe.paraphrase(updated)
            features = normalize(pipe.get_text_features(updated), dim=-1)
            order = (features @ pipe.img_embs.T).flatten().argsort().flip(0)
            rewrites[answer] = (updated, order[:10].tolist())
        updated, top10 = rewrites[answer]
        outcomes.append(dict(image_id=image_id, answer=answer, rewrite=updated, hit=image_id in top10))
    shared.atomic_json(path, outcomes)
    return outcomes


def run_one(pipe, arm, task, inputs, position):
    qid = task['qid']
    history = []
    audit = []
    generate = shared.question_candidates
    original = pipe.select_question

    def proposals(model, dialogue, query, evidence):
        history[:] = dialogue
        return generate(model, dialogue, query, evidence)

    def choose(**kw):
        usual = original(**kw)
        ranks = sample_ranks(qid, kw['round'])
        order = kw['cossim_prev'].argsort().flip(0)
        proxies = [int(order[r - 1]) for r in ranks]
        questions = list(dict.fromkeys(kw['questions']))
        chosen = usual
        scores = {}
        if len(questions) > 1:
            with isolated_rng(pipe.client):
                for q in questions:
                    results = score_question(pipe, history, kw['caption_recon'], q, proxies)
                    scores[q] = sum(x['hit'] for x in results)
            best = max(scores.values())
            tied = [q for q in questions if scores[q] == best]
            if usual not in tied:
                chosen = tied[0] if len(tied) == 1 else original(**{**kw, 'questions': tied})
        audit.append(dict(ranks=ranks, image_ids=proxies, scores=scores, incumbent=usual, chosen=chosen))
        return chosen

    pipe.client.next_seed = 1021 + qid
    with patch.object(shared, 'ROOT', inputs), patch.object(shared, 'question_candidates', proposals), patch.object(
            pipe, 'select_question', choose):
        row = shared.run_dialogue(pipe, ARMS[arm], task, position, hits_only=True)
    return dict(row, arm=arm, proxy_choices=audit)


def validate(row, ref):
    if (row['target'], row['queries'][0], row['ranks'][0]) != (
            ref['target'], ref['queries'][0], ref['ranks'][0]):
        raise ValueError('Mismatched initial state')
    prefix = True
    for j, (decision, turn) in enumerate(zip(row['proxy_choices'], row['turns'], strict=True)):
        scores = decision['scores']
        choice = decision['chosen']
        if (decision['ranks'] != sample_ranks(row['qid'], j+1) or len(set(decision['image_ids'])) != 10
                or choice != turn['question'] or choice not in turn['candidate_questions']
                or (scores and scores[choice] != max(scores.values()))):
            raise ValueError(f'Invalid selection in dialogue {row["qid"]}, question {j+1}')
        if prefix:
            old = ref['turns'][j]
            if (any(turn[k] != old[k] for k in ('query_before', 'candidate_questions', 'selected_ids'))
                    or decision['incumbent'] != old['question']):
                raise ValueError(f'Control diverged before selection: {row["qid"]}, question {j+1}')
            prefix = choice == old['question']
            if prefix and (turn['query_after'] != old['query_after'] or turn['answer'] != old['answer']
                           or row['ranks'][j+1] != ref['ranks'][j+1]):
                raise ValueError(f'Control drift after identical choice: {row["qid"]}, question {j+1}')


def report(rows, controls):
    if not rows:
        return {'n': 0}
    stats = paired_summary(rows, controls)
    pairs = stats['first_hit_control_variant'].values()
    stats['questions_control_variant'] = [sum(min(pair[side], 10) for pair in pairs) for side in (0, 1)]
    stats['changed_choices'] = sum(d['incumbent'] != d['chosen'] for r in rows.values() for d in r['proxy_choices'])
    return stats


@torch.inference_mode()
def main(check_only):
    groups, vanilla, mmr = preflight()
    controls = dict(mmr85_proxy=mmr, vanilla_proxy=vanilla)
    splits = shared.read_json(ROOT/'output/mmr-within-round-questions/plan.json')['splits']
    ids = [i for group in splits for i in group]
    if list(map(len, splits)) != [20, 20] or len(set(ids)) != 40 or any(mmr[i]['ranks'][0] <= 10 for i in ids):
        raise ValueError('Expected the two frozen groups of initial misses')
    tasks = {t['qid']: (t, folder) for _, group, _, folder in groups for t in group}
    sources = [Path(__file__), WORKER, GRID_WORKER, Path(shared.__file__), Path(mac.__file__), mac.HERE/'generate_dialog.py']
    plan = dict(splits=splits, arms=ARMS, seed=SEED, proxy_ranks='one per band of four at ranks 11-50',
                metric='same image enters full-corpus top 10', ties='original KL among highest scorers',
                mmr=.85, corpus=50000, max_questions=10, model=SNAPSHOT.name, baseline_proposals=5,
                primary='user questions through first hit, capped at ten; report paired R1/R3/R5/R10',
                controls={arm:digest([refs[i] for i in ids]) for arm, refs in controls.items()},
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Previous checkpoints use a different plan')
    print('Validated 2 matched groups of 20, both controls, ten sampled images per question slate.', flush=True)
    if check_only:
        return
    OUT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as session:
        lock = session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shared.atomic_json(path, plan)
        rows = {arm:shared.read_records(OUT, arm) for arm in ARMS}
        for arm, data in rows.items():
            if not data.keys() <= set(ids):
                raise ValueError('Unexpected checkpoint IDs')
            for i, row in data.items():
                validate(row, controls[arm][i])
        session.enter_context(patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)))
        pipe = None
        # Alternate matched arms so interrupted runs still have comparable partial results.
        for i in ids:
            for arm in ARMS:
                if i in rows[arm]:
                    continue
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Launch the pilot in Mac Terminal for Metal access.')
                    with patch.object(mac, '__file__', str(WORKER)):
                        client = session.enter_context(closing(mac.MacQwen(SNAPSHOT, OUT)))
                    pipe = mac.load_blip(INPUTS, client)
                    positions = {img:j for j,img in enumerate(pipe.search_space)}
                task, folder = tasks[i]
                print(f'Starting {arm} dialogue {i}', flush=True)
                row = run_one(pipe, arm, task, folder, positions[task['img']])
                validate(row, controls[arm][i])
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
                rows[arm][i] = row
                print(f'{arm}: {len(rows[arm])}/40 saved', flush=True)
            summary = {}
            for arm, data in rows.items():
                summary[arm] = dict(all=report(data, controls[arm]), groups=[
                    report({k:data[k] for k in group if k in data}, controls[arm]) for group in splits])
            shared.atomic_json(OUT/'summary.json', summary)
        for arm in ARMS:
            stats = summary[arm]['all']
            print(arm, 'baseline / pilot R1, R3, R5, R10:',
                  [(stats['control'][r], stats['variant'][r]) for r in (1,3,5,10)],
                  'questions:', stats['questions_control_variant'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        main(parser.parse_args().check_only)
