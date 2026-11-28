"""Extend the archived vanilla and plain MMR 0.85 comparison to all VisDial validation IDs."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
from pathlib import Path
import random
from unittest.mock import patch

from guard_history_veto import guard_metal_timeout
from run_matan_400 import preflight
from run_remaining_hits import OUTPUT as OLD_EXTENSION
from run_overnight_hits import INPUTS, OLD_BASELINE, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared, torch

OUT = ROOT / 'output/matched-vanilla-mmr85'
PROXY_LOCK = ROOT / 'output/proxy-image-retrieval/run.lock'
SEED = 20261127
METHODS = {'vanilla': 'vanilla', 'mmr85': 's3_current_mmr'}


def validate(row, task):
    ranks = row['ranks']
    if (row['qid'] != task['qid'] or row['target'] != task['img'] or row['queries'][0] != task['caption']
            or not row['hits_only'] or len(row['queries']) != len(ranks)
            or len(row['turns']) + 1 != len(ranks) or not 1 <= len(ranks) <= 11
            or not all(1 <= r <= 50000 for r in ranks) or any(r <= 10 for r in ranks[:-1])
            or (len(ranks) < 11 and ranks[-1] > 10)):
        raise ValueError(f'Unmatched or incomplete record for {task["qid"]}')


def prepare():
    _, vanilla400, mmr = preflight()
    rows = {'vanilla': {}, 'mmr85': mmr}
    vanilla = rows['vanilla']
    for folder in (OLD_BASELINE, OVERNIGHT_OUTPUT, OLD_EXTENSION):
        batch = shared.read_records(folder, 'vanilla')
        if vanilla.keys() & batch.keys():
            raise ValueError('Overlapping archived vanilla IDs')
        vanilla.update(batch)
    if len(vanilla) != 941 or mmr.keys() != vanilla400.keys() or any(
            vanilla[i]['ranks'] != vanilla400[i]['ranks'] for i in mmr):
        raise ValueError('Expected 941 archived vanilla dialogues and their original 400 matched IDs')
    data = shared.read_json(REPO/'dialogues/VisDial_v1.0_queries_val.json')
    tasks = {i:dict(qid=i, img=d['img'], caption=d['dialog'][0]) for i,d in enumerate(data)}
    corpus = set(shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json'))
    target_dir = OVERNIGHT_OUTPUT/'fresh-inputs/targets'
    if len(tasks) != 2064 or target_dir.resolve() != (REPO/'data').resolve() or any(
            t['img'] not in corpus or not (target_dir/t['img']).is_file() for t in tasks.values()):
        raise ValueError('Full validation inputs do not match the archived retrieval corpus')
    for records in rows.values():
        for i,row in records.items():
            validate(row, tasks[i])
    catchup = sorted(vanilla.keys() - mmr.keys())
    fresh = sorted(tasks.keys() - vanilla.keys())
    rng = random.Random(SEED)
    rng.shuffle(catchup)
    rng.shuffle(fresh)
    sources = [Path(__file__), Path(shared.__file__), Path(mac.__file__), mac.HERE/'generate_dialog.py']
    plan = dict(original=sorted(mmr), catchup=catchup, fresh=fresh, seed=SEED, snapshot=SNAPSHOT.name,
                corpus=50000, max_questions=10, mmr_weight=.85, arms=METHODS,
                first_milestone=1000, stop_after=len(tasks),
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('The frozen run plan differs from the current configuration')
    for arm, data in rows.items():
        saved = shared.read_records(OUT, arm)
        if data.keys() & saved.keys():
            raise ValueError(f'Duplicate archived and new {arm} records')
        for i,row in saved.items():
            validate(row, tasks[i])
        data.update(saved)
    verify_pairs(rows)
    if not path.exists():
        shared.atomic_json(path, plan)
    return plan, tasks, rows


def verify_pairs(rows):
    vanilla, mmr = rows['vanilla'], rows['mmr85']
    if not mmr.keys() <= vanilla.keys() or any(mmr[i]['ranks'][0] != vanilla[i]['ranks'][0] for i in mmr):
        raise ValueError('MMR checkpoints must match vanilla at round zero')


def metrics(ids, rows):
    stats = {'n': len(ids)}
    for arm, records in rows.items():
        first = [next((j for j,r in enumerate(records[i]['ranks']) if r <= 10), 11) for i in ids]
        stats[arm] = dict(cumulative_hits_at_10=[sum(j <= r for j in first) for r in range(11)],
                          questions=sum(min(j,10) for j in first))
    return stats


def save_summary(ids, rows, original, path):
    shared.atomic_json(path, dict(all=metrics(ids, rows), original_400=metrics(original, rows),
                                  additional=metrics([i for i in ids if i not in original], rows)))


def extend(plan, tasks, rows, play):
    order = plan['original'] + plan['catchup'] + plan['fresh']
    original = set(plan['original'])
    hit_milestone = plan['first_milestone']
    for n,i in enumerate(order, 1):
        if i in original:
            continue
        for arm, records in rows.items():
            if i in records:
                continue
            vanilla = rows['vanilla'].get(i)
            if arm == 'mmr85' and vanilla['ranks'][0] <= 10:
                row = dict(vanilla, execution='shared initial retrieval; zero questions')
            else:
                row = play(arm, tasks[i])
            row['arm'] = arm
            validate(row, tasks[i])
            if arm == 'mmr85' and row['ranks'][0] != vanilla['ranks'][0]:
                raise ValueError(f'Initial rank mismatch at ID {i}')
            shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
            records[i] = row
            print(f'{arm} saved ID {i}; {len(rows["mmr85"])}/{len(tasks)} matched', flush=True)
        if n == hit_milestone and not (OUT/'matched-1000.json').exists():
            save_summary(order[:n], rows, original, OUT/'matched-1000.json')
            print(f'{n} matched comparison saved. Continuing to {len(tasks)}.', flush=True)
        if n == len(tasks) or (n % 25 == 0 and n == len(rows['mmr85'])):
            save_summary(order[:n], rows, original, OUT/'summary.json')


def wait_for_proxy(session):
    if not PROXY_LOCK.exists():
        return
    lock = session.enter_context(PROXY_LOCK.open('r'))
    try:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Waiting for the proxy pilot to finish or be stopped in its own Terminal.', flush=True)
        fcntl.flock(lock, fcntl.LOCK_SH)


@torch.inference_mode()
def run(check_only):
    OUT.mkdir(exist_ok=True, parents=True)
    with ExitStack() as session:
        lock = session.enter_context((OUT/'run.lock').open('a'))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('The matched queue already has a running or waiting launcher.')
        plan, tasks, rows = prepare()
        print(f'{len(rows["vanilla"])} vanilla, {len(rows["mmr85"])} matched; milestone 1000, finish {len(tasks)}.', flush=True)
        if check_only:
            return
        pipe = None
        positions = {}
        session.enter_context(patch.object(shared, 'ROOT', OVERNIGHT_OUTPUT/'fresh-inputs'))
        session.enter_context(patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)))

        def play(arm, task):
            nonlocal pipe
            if pipe is None:
                wait_for_proxy(session)
                if not torch.backends.mps.is_available():
                    raise SystemExit('Launch the command from Mac Terminal for Metal access.')
                client = session.enter_context(closing(mac.MacQwen(SNAPSHOT, OUT)))
                pipe = mac.load_blip(INPUTS, client)
                positions.update((img,j) for j,img in enumerate(pipe.search_space))
            pipe.client.next_seed = 1021 + task['qid']
            return shared.run_dialogue(pipe, METHODS[arm], task, positions[task['img']], hits_only=True)

        extend(plan, tasks, rows, play)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
