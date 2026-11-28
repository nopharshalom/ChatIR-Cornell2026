"""One-step counterfactual screen on all first-round slates of the frozen 40."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

import run_near_miss_question_pilot as probe
from guard_history_veto import guard_metal_timeout
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, preflight, shared

OUT = ROOT / 'output/mmr-first-round-question-screen'


def similarity(pipe, query):
    return normalize(pipe.get_text_features(query), dim=-1) @ pipe.img_embs.T


def select(pipe, turn, initial_scores):
    query = turn['query_before']
    questions = turn['candidate_questions']
    kl = pipe.select_question(query, questions, initial_scores, 10, pipe.img_embs, 500, 1)
    if kl != turn['question']:
        raise ValueError('KL failed to reproduce the saved choice')
    near = set(initial_scores.argsort().flip(0)[10:50].tolist())
    candidates = []
    for question in questions:
        branches = probe.answer_branches(pipe, query, turn['selected_captions'], question)
        score = None
        if branches:
            entrants = []
            for b in branches:
                order = similarity(pipe, b['caption']).flatten().argsort().flip(0)
                b['top10'] = order[:10].tolist()
                entrants.append(near.intersection(b['top10']))
            score = len(entrants[0] ^ entrants[1])
        candidates.append(dict(question=question, score=score, branches=branches))
    high = max((r['score'] for r in candidates if r['score'] is not None), default=0)
    best = [r['question'] for r in candidates if r['score'] == high]
    choice = best[0] if high > 0 and len(best) == 1 else kl
    return dict(kl=kl, probe=choice, candidates=candidates)


def evaluate(pipe, ref, target_image, position):
    turn = ref['turns'][0]
    query = turn['query_before']
    scores = similarity(pipe, query).flatten()
    if int(shared.inverse_ranks(scores.argsort().flip(0))[position]) != ref['ranks'][0]:
        raise ValueError(f"Initial rank differs for {ref['qid']}")
    choice = select(pipe, turn, scores)
    outcomes = {}
    for q in turn['candidate_questions']:
        # Same state and random seed for each candidate, independent of list order.
        pipe.client.next_seed = 1021 + ref['qid']
        answer = pipe.client.answer(target_image, q)
        updated = pipe.reconstruct_dialog([query, f'{q} {answer}'])
        if updated == query:
            updated = pipe.paraphrase(updated)
        order = similarity(pipe, updated).flatten().argsort().flip(0)
        outcomes[q] = dict(answer=answer, rewrite=updated, rank=int(shared.inverse_ranks(order)[position]))
    return dict(qid=ref['qid'], selection=choice, outcomes=outcomes, saved_kl_rank=ref['ranks'][1])


def summarize(records):
    kl_hits, probe_hits, possible, rescues, losses, drift = [], [], [], [], [], []
    for r in records:
        qid, selection, outcomes = r['qid'], r['selection'], r['outcomes']
        kl_rank = outcomes[selection['kl']]['rank']
        kl = kl_rank <= 10
        hit = outcomes[selection['probe']]['rank'] <= 10
        if kl:
            kl_hits.append(qid)
        if hit:
            probe_hits.append(qid)
        if any(v['rank'] <= 10 for v in outcomes.values()):
            possible.append(qid)
        if hit and not kl:
            rescues.append(qid)
        if kl and not hit:
            losses.append(qid)
        if kl_rank != r['saved_kl_rank']:
            drift.append(qid)
    return dict(n=len(records), kl_hits=len(kl_hits), probe_hits=len(probe_hits),
                any_question_hits=len(possible), recoverable_kl_misses=sorted(set(possible)-set(kl_hits)),
                probe_rescues=rescues, probe_losses=losses, kl_rank_replay_differences=drift)


@torch.inference_mode()
def run(check_only):
    groups, _, _ = preflight()
    targets = {task['qid']: folder/'targets'/task['img']
               for _, group, _, folder in groups for task in group}
    splits = shared.read_json(probe.base.OUT/'plan.json')['splits']
    refs = shared.read_records(probe.base.OUT, 'retry_fix_unique')
    ids = [i for group in splits for i in group]
    if list(map(len, splits)) != [20, 20] or len(set(ids)) != 40 or refs.keys() != set(ids):
        raise ValueError('Expected exactly the two completed groups of 20')
    for i, ref in refs.items():
        t = ref['turns'][0]
        qs = t['candidate_questions']
        if (not ref.get('finished_utc') or ref['ranks'][0] <= 10 or len(qs) != len(set(map(probe.base.key,qs)))
                or not 1 <= len(qs) <= 5 or t['question'] not in qs or t['query_before'] != ref['queries'][0]
                or not targets[i].is_file() or targets[i].name != Path(ref['target']).name):
            raise ValueError(f'Invalid saved first-round state for {i}')
    files = [Path(__file__), Path(probe.__file__), Path(probe.base.__file__), GRID_WORKER,
             Path(mac.__file__), Path(shared.__file__), mac.HERE/'generate_dialog.py']
    plan = dict(splits=splits, round=1, model=SNAPSHOT.name, corpus=50000,
                selector='unique positive maximum of branch-exclusive promotions from 11-50; else KL',
                evaluator='matched image answer, reconstruction, paraphrase on exact repeat, full corpus rank',
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                controls=hashlib.sha256(json.dumps(refs,sort_keys=True).encode()).hexdigest())
    total = sum(len(r['turns'][0]['candidate_questions']) for r in refs.values())
    print(f'Validated {len(ids)} dialogues and {total} first-round candidate questions.', flush=True)
    if check_only:
        return
    OUT.mkdir(exist_ok=True, parents=True)
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path) != plan:
            raise ValueError('Saved screen belongs to another plan')
        shared.atomic_json(path, plan)
        records = {}
        for f in (OUT/'cases').glob('*.json'):
            r = shared.read_json(f)
            i = int(f.stem)
            if (i not in refs or r['qid'] != i or set(r['outcomes']) != set(refs[i]['turns'][0]['candidate_questions'])
                    or any(not 1 <= v['rank'] <= 50000 for v in r['outcomes'].values())):
                raise ValueError(f'Invalid checkpoint {f}')
            records[i] = r
        if len(records) < len(ids):
            with patch.object(mac, '__file__', str(GRID_WORKER)):
                client = mac.MacQwen(SNAPSHOT, OUT)
            with closing(client):
                pipe = mac.load_blip(INPUTS, client)
                positions = {name:i for i,name in enumerate(pipe.search_space)}
                for i in ids:
                    if i in records:
                        continue
                    ref = refs[i]
                    row = evaluate(pipe, ref, targets[i], positions[ref['target']])
                    shared.atomic_json(OUT/'cases'/f'{i}.json', row)
                    records[i] = row
                    print(f'{len(records)}/40 saved (dialogue {i})', flush=True)
        result = dict(all=summarize([records[i] for i in ids]),
                      groups=[summarize([records[i] for i in group]) for group in splits])
        shared.atomic_json(OUT/'summary.json', result)
        print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
