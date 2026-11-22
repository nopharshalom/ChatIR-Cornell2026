"""Finish the VisDial validation set with vanilla and all-query S2 on the local Mac."""
import argparse
import fcntl
import hashlib
from pathlib import Path

from run_overnight_hits import (
    INPUTS, OLD_BASELINE, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, SOURCE,
    mac, shared, summarize, torch,
)

ARMS = ('vanilla', 's2_history_pool')
OUTPUT = ROOT / 'output/agentir-full-vanilla-s2'


def prepare(output):
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    prior = shared.read_json(OVERNIGHT_OUTPUT / 'plan.json')
    if (len(data), prior['snapshot'], prior['max_rounds'], prior['hits_only']) != (
            2064, SNAPSHOT.name, shared.ROUNDS, True):
        raise ValueError('Validation set or previous run settings differ')
    for name, expected in prior['fresh_source'].items():
        if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Archived runner changed: {name}')
    tasks = {i: dict(qid=i, img=row['img'], caption=row['dialog'][0]) for i, row in enumerate(data)}
    corpus = set(shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json'))
    for task in tasks.values():
        if task['img'] not in corpus or not (REPO / 'data' / task['img']).is_file():
            raise ValueError(f'Missing candidate or target image for {task["qid"]}')
    previous = {arm: {} for arm in ARMS}
    for batch, directory, expected in [('original', OLD_BASELINE, 200), ('fresh', OVERNIGHT_OUTPUT, 300)]:
        ids = {task['qid'] for task in prior[batch]}
        for arm in ARMS:
            rows = shared.read_records(directory, arm)
            if len(ids) != expected or rows.keys() != ids or ids.intersection(previous[arm]):
                raise ValueError(f'Unexpected IDs in {directory / arm}')
            previous[arm].update(rows)
    remaining = sorted(tasks.keys() - previous['vanilla'].keys())
    plan = dict(arms=list(ARMS), remaining=remaining, snapshot=SNAPSHOT.name, max_rounds=shared.ROUNDS,
                hits_only=True, source_sha256=prior['fresh_source'])
    plan_file = output / 'plan.json'
    if plan_file.exists() and shared.read_json(plan_file) != plan:
        raise ValueError('Saved plan differs from the current plan')
    saved = {arm: shared.read_records(output, arm) for arm in ARMS}
    for arm in ARMS:
        if saved[arm].keys() - set(remaining):
            raise ValueError(f'Unexpected saved IDs for {arm}')
        for qid, row in (previous[arm] | saved[arm]).items():
            if (row['target'] != tasks[qid]['img'] or row['queries'][0] != tasks[qid]['caption']
                    or not row.get('hits_only')):
                raise ValueError(f'Checkpoint does not match dialogue {qid} for {arm}')
    if not plan_file.exists():
        shared.atomic_json(plan_file, plan)
    return [tasks[i] for i in remaining], previous, saved


def run_pairs(output, tasks, saved, pipe):
    positions = {name: i for i, name in enumerate(pipe.search_space)}
    for i, task in enumerate(tasks, 1):
        qid = task['qid']
        for arm in ARMS:
            if qid in saved[arm]:
                continue
            pipe.client.next_seed = 1021 + qid
            row = shared.run_dialogue(pipe, arm, task, positions[task['img']], hits_only=True)
            if arm != 'vanilla' and row['ranks'][0] != saved['vanilla'][qid]['ranks'][0]:
                raise ValueError(f'Initial rank mismatch for {qid}')
            shared.atomic_json(output / arm / 'dialogues' / f'{qid}.json', row)
            saved[arm][qid] = row
            print(f'{i}/{len(tasks)} · {arm} · qid {qid} saved · {len(row["turns"])} questions', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True, parents=True)
    with (OUTPUT / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('The full-set queue is already running.')
        tasks, previous, saved = prepare(OUTPUT)
        print(f'500 completed dialogues reused. Remaining set: {len(tasks)}. '
              f'Already saved here: vanilla {len(saved["vanilla"])}, S2 {len(saved["s2_history_pool"])}.',
              flush=True)
        shared.ROOT = OVERNIGHT_OUTPUT / 'fresh-inputs'
        if (shared.ROOT / 'targets').resolve() != (REPO / 'data').resolve():
            raise ValueError('The target image directory differs from the previous run.')
        if args.prepare_only:
            return
        if any(len(saved[arm]) != len(tasks) for arm in ARMS):
            if not torch.backends.mps.is_available():
                raise SystemExit('Launch from macOS Terminal for Metal access.')
            qwen = mac.MacQwen(SNAPSHOT, OUTPUT)
            try:
                run_pairs(OUTPUT, tasks, saved, mac.load_blip(INPUTS, qwen))
            finally:
                qwen.close()
        combined = {arm: previous[arm] | saved[arm] for arm in ARMS}
        summary = {}
        for arm, rows in combined.items():
            curve = summarize(rows, combined['vanilla'])
            summary[arm] = dict(by_round=curve, questions=sum(len(row['turns']) for row in rows.values()))
            print(f'{arm}: {curve[10]["hits"]}/{len(rows)} Hits@10 after ten rounds, '
                  f'{summary[arm]["questions"]} questions')
        shared.atomic_json(OUTPUT / 'summary.json', summary)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nPaused. Relaunch the same command to resume from saved dialogues.')
        raise SystemExit(130)
