"""Run the two S3 selectors concurrently with isolated checkpoints and model workers."""

import argparse
from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
from shutil import copy2
import subprocess
from tempfile import TemporaryDirectory
import time


PACKAGE = Path(__file__).resolve().parents[1]
ARMS = ('s3_current_top10', 's3_history_mmr')


def run(repo, mlx_env, snapshot, results):
    started = time.monotonic()
    sample = json.loads((PACKAGE / 'sample.json').read_text())['tasks']
    python = repo / '.venv/bin/python'
    archive = 'agentir_full_dialogue_bundle_v2.zip'
    results.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        lock = stack.enter_context((results / '.run.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        workers = {}
        for arm in ARMS:
            stage = Path(stack.enter_context(TemporaryDirectory(prefix=f'agentir-{arm}-')))
            bundle = stage / 'deliverables/full_dialogue_colab'
            source = bundle / 'source'
            source.mkdir(parents=True)
            (stage / 'ChatIR-Cornell2026').symlink_to(repo, target_is_directory=True)
            (stage / 'tmp').mkdir()
            (stage / 'tmp/agentir-mlx-vlm-env').symlink_to(mlx_env, target_is_directory=True)
            for name in ('local_hits.py', 'local_pilot.py', 'run_dialogues.py', 'generate_dialog.py',
                         'runtime.py', 'text_stops.py'):
                copy2(PACKAGE / 'source' / name, source / name)
            (bundle / archive).symlink_to(PACKAGE / archive)
            work = results / arm
            work.mkdir(exist_ok=True)
            log = stack.enter_context((work / 'run.log').open('a'))
            command = [str(python), '-u', str(source / 'local_hits.py'), '--snapshot',
                       str(snapshot), '--output', str(work), '--limit', str(len(sample)), '--arms', arm]
            workers[arm] = stack.enter_context(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT))
        codes = {arm: process.wait() for arm, process in workers.items()}
        if any(codes.values()):
            raise SystemExit(f'Selector process exit codes: {codes}. See each selector’s run.log.')
        timing = results / 'parallel_timing.json'
        temporary = timing.with_suffix('.tmp')
        temporary.write_text(json.dumps({'wall_seconds_this_invocation': time.monotonic() - started,
                                        'exit_codes': codes}, indent=2) + '\n')
        temporary.replace(timing)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'mlx-env', 'snapshot', 'results'):
        parser.add_argument(f'--{name}', required=True, type=lambda path: Path(path).resolve())
    args = parser.parse_args()
    run(args.repo, args.mlx_env, args.snapshot, args.results)
