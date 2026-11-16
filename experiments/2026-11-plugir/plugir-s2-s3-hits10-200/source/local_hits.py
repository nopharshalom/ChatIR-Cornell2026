"""Run matched PlugIR experiments on a Mac, stopping each task at its first top-ten hit."""
import argparse
import hashlib
from pathlib import Path
import time
from zipfile import ZipFile

import torch

import local_pilot as mac
import run_dialogues as shared


def run(snapshot, out, arms, limit):
    started = time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    inputs = mac.PROJECT / 'tmp/agentir-local-9b-inputs'
    with ZipFile(mac.HERE.parent / 'agentir_full_dialogue_bundle_v2.zip') as bundle:
        for entry in bundle.namelist():
            if entry == 'sample.json' or entry.startswith(('Protocol/', 'cache/', 'targets/')):
                bundle.extract(entry, inputs)
    shared.ROOT = inputs
    tasks = shared.read_json(inputs / 'sample.json')['tasks']
    selected = tasks[:limit]
    config_path = out / 'config.json'
    config = dict(
        model=mac.MODEL, revision=snapshot.name, backend='MLX 4-bit', hits_only=True,
        retrieval='BLIP float32 on MPS', corpus_size=50_000, max_rounds=10,
        arms=arms, sample_ids=[t['qid'] for t in selected], rrf_k=60, mmr_weight=.7,
        stop_at_rank=10, report_metrics=['Hits@10'],
        source_sha256={
            name: hashlib.sha256((mac.HERE / name).read_bytes()).hexdigest()
            for name in ('local_hits.py', 'local_pilot.py', 'run_dialogues.py', 'generate_dialog.py')
        })
    if config_path.exists() and shared.read_json(config_path) != config:
        raise ValueError('Run settings differ. Use a new Hits-only output folder.')
    shared.atomic_json(config_path, config)

    records = {arm: shared.read_records(out, arm) for arm in arms}
    todo = [(arm, task) for start in range(0, limit, 10) for arm in arms
            for task in selected[start:start + 10] if task['qid'] not in records[arm]]
    if not todo:
        print('All requested checkpoints already exist.')
        return
    qwen = mac.MacQwen(snapshot, out)
    try:
        torch.set_grad_enabled(False)
        pipeline = mac.load_blip(inputs, qwen)
        ids = {name: i for i, name in enumerate(pipeline.search_space)}
        setup_seconds = time.perf_counter() - started
        print(f'Models and index loaded in {setup_seconds:.1f}s', flush=True)
        for arm, task in todo:
            qwen.next_seed = 1021 + task['qid']
            rec = shared.run_dialogue(pipeline, arm, task, ids[task['img']], hits_only=True)
            shared.atomic_json(out / arm / 'dialogues' / f'{task["qid"]}.json', rec)
            records[arm][task['qid']] = rec
            print(f'{arm} saved {task["qid"]} · {len(rec["turns"])} turns · {rec["seconds"]:.1f}s',
                  flush=True)
        shared.atomic_json(out / 'timing.json', dict(
            elapsed_this_invocation=time.perf_counter() - started, setup_seconds=setup_seconds,
            completed_new=len(todo), **qwen.metadata))
    finally:
        qwen.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arms', choices=shared.ARMS, nargs='+', default=list(shared.ARMS))
    parser.add_argument('--limit', type=int, choices=range(1, 201), default=200, metavar='1–200')
    args = parser.parse_args()
    run(args.snapshot, args.output, list(dict.fromkeys(args.arms)), args.limit)
