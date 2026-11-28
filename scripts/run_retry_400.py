"""Four sequential, checkpointed retry comparisons on the archived matched 400."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import torch

import run_question_diversity as pilot
from guard_history_veto import guard_metal_timeout
from run_a2r_unseen_pilots import paired_summary
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, preflight, shared

OUT = ROOT / 'output/retry-400'
# Finish and summarize each arm before starting inference on the next.
QUEUE = (
    ('mmr85_retry_unique', 's3_current_mmr', True),
    ('mmr85_retry', 's3_current_mmr', False),
    ('vanilla_retry_unique', 'vanilla', True),
    ('vanilla_retry', 'vanilla', False),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def pilot_checkpoints(mmr):
    p = shared.read_json(pilot.OUT/'plan.json')
    ids = [i for split in p['splits'] for i in split]
    if (len(ids) != 40 or len(set(ids)) != 40 or p['model'] != SNAPSHOT.name or p['mmr'] != .85
            or p['controls'] != digest([mmr[i] for i in ids])):
        raise ValueError('The archived pilot does not match the MMR controls')
    paths = [Path(pilot.__file__), Path(pilot.__file__).with_name('run_history_veto.py'),
             Path(shared.__file__), Path(mac.__file__), mac.HERE/'generate_dialog.py', GRID_WORKER]
    if {s.name: hashlib.sha256(s.read_bytes()).hexdigest() for s in paths} != p['sources']:
        raise ValueError('Pilot source differs from archived settings')
    arms = {}
    control = mmr
    for key in ('retry_fix', 'retry_fix_unique'):
        rows = shared.read_records(pilot.OUT, key)
        if set(ids) != rows.keys():
            raise ValueError(f'Pilot missing matched IDs for {key}')
        for i, row in rows.items():
            pilot.validate(row, mmr[i], control[i], key)
        arms[key] = rows
        control = rows
    return arms


def validate(row, ref, arm, unique):
    ranks, turns = row['ranks'], row['turns']
    if (row['qid'] != ref['qid'] or row['arm'] != arm or not row.get('hits_only')
            or (row['target'], row['queries'][0], ranks[0]) != (ref['target'], ref['queries'][0], ref['ranks'][0])
            or not 1 <= len(ranks) <= 11 or len(turns)+1 != len(ranks)
            or len(row['queries']) != len(ranks) or len(row['proposal_audit']) != len(turns)
            or any(r<=10 for r in ranks[:-1]) or (len(ranks)<11 and ranks[-1]>10)):
        raise ValueError(f'Invalid or unmatched {arm} dialogue {ref["qid"]}')
    for turn in turns:
        qs = turn['candidate_questions']
        if (turn['question'] not in qs or not 1 <= len(qs) <= 5
                or (unique and len(set(map(pilot.key,qs))) != len(qs))
                or (not unique and len(qs) != 5)):
            raise ValueError(f'Invalid question slate in {arm} dialogue {ref["qid"]}')


def seed_rows(arm, method, unique, refs, saved):
    rows = shared.read_records(OUT, arm)
    if not rows.keys() <= refs.keys():
        raise ValueError(f'Unexpected checkpoint IDs in {arm}')
    cached = saved['retry_fix_unique' if unique else 'retry_fix'] if method == 's3_current_mmr' else {}
    for i, ref in refs.items():
        if i in rows:
            validate(rows[i], ref, arm, unique)
            continue
        if ref['ranks'][0] <= 10:
            row = dict(ref, arm=arm, proposal_audit=[], execution='initial caption hit')
        elif i in cached:
            row = dict(cached[i], arm=arm, execution='matched 40-dialogue pilot')
        else:
            continue
        validate(row, ref, arm, unique)
        shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
        rows[i] = row
    return rows


def play(pipe, method, unique, arm, task, inputs, position):
    audit = []
    shared.ROOT = inputs
    pipe.client.next_seed = 1021 + task['qid']
    candidates = partial(pilot.proposals, unique=unique, audit=audit)
    with patch.object(shared, 'question_candidates', candidates):
        row = shared.run_dialogue(pipe, method, task, position, hits_only=True)
    return dict(row, arm=arm, proposal_audit=audit, execution='live')


def compare(rows, refs, pilot_ids):
    if rows.keys() != refs.keys() or len(rows) != 400:
        raise ValueError('A full comparison requires the identical 400 IDs')
    outside = {i:row for i,row in rows.items() if i not in pilot_ids}
    return dict(all=paired_summary(rows, refs), outside_pilot=paired_summary(outside, refs),
                questions_asked=sum(len(row['turns']) for row in rows.values()),
                control_questions=sum(len(row['turns']) for row in refs.values()))


@torch.inference_mode()
def run(prepare_only):
    groups, vanilla, mmr = preflight()
    saved = pilot_checkpoints(mmr)
    pilot_ids = set(saved['retry_fix'])
    refs_for = {'s3_current_mmr': mmr, 'vanilla': vanilla}
    tasks = [(task, inputs) for _, cohort, _, inputs in groups for task in cohort]
    cohorts = {name:[task['qid'] for task in cohort] for name,cohort,_,_ in groups}
    plan = dict(queue=[name for name,_,_ in QUEUE], cohorts=cohorts, pilot_ids=sorted(pilot_ids),
                corpus=50000, max_questions=10, stop_at_rank=10, mmr_weight=.85,
                model=SNAPSHOT.name, slots=5, attempts_per_slot=3, selector='original minimum KL',
                protocol='raw full-corpus Hits@10; current-query shrinking evidence pool; caption evidence',
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
                         [Path(__file__), Path(pilot.__file__), Path(shared.__file__), Path(mac.__file__),
                          mac.HERE/'generate_dialog.py', GRID_WORKER]},
                baseline_hashes={name:digest(rows) for name,rows in refs_for.items()},
                pilot_hashes={name:digest(rows) for name,rows in saved.items()})
    OUT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as session:
        lock = session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path) != plan:
            raise ValueError('Output already belongs to a different queue configuration')
        shared.atomic_json(path, plan)
        arms = {}
        for name,method,unique in QUEUE:
            arms[name] = seed_rows(name,method,unique,refs_for[method],saved)
            print(f'{name}: {len(arms[name])}/400 checkpointed', flush=True)
        if prepare_only:
            return
        pipe = None
        session.enter_context(patch.object(shared,'history_mmr',partial(shared.history_mmr,weight=.85)))
        reports = {}
        for name,method,unique in QUEUE:
            rows, refs = arms[name], refs_for[method]
            for task, inputs in tasks:
                i = task['qid']
                if i in rows:
                    continue
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Metal unavailable. Start Run_Retry_400.command in macOS Terminal.')
                    with patch.object(mac,'__file__',str(GRID_WORKER)):
                        worker = session.enter_context(closing(mac.MacQwen(SNAPSHOT, OUT)))
                    pipe = mac.load_blip(INPUTS, worker)
                    positions = {img:k for k,img in enumerate(pipe.search_space)}
                print(f'{name}: starting {i}', flush=True)
                row = play(pipe,method,unique,name,task,inputs,positions[task['img']])
                validate(row,refs[i],name,unique)
                shared.atomic_json(OUT/name/'dialogues'/f'{i}.json',row)
                rows[i] = row
                print(f'{name}: {len(rows)}/400 checkpointed', flush=True)
            report = compare(rows, refs, pilot_ids)
            reports[name] = report
            shared.atomic_json(OUT/name/'summary.json',report)
            curve = report['all']['variant']
            print(f'{name} complete. Cumulative hits at R1/R3/R5/R10: '
                  f'{curve[1]}/{curve[3]}/{curve[5]}/{curve[10]}', flush=True)
        for prefix in ('mmr85','vanilla'):
            reports[prefix+'_replacement_vs_retry'] = compare(
                arms[prefix+'_retry_unique'],arms[prefix+'_retry'],pilot_ids)
        shared.atomic_json(OUT/'summary.json',reports)
        print('All four arms complete on the same 400 dialogues.',flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().prepare_only)
