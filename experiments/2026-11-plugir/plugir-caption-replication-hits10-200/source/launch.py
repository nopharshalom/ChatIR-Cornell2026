"""Run the three replication arms with two concurrent GPU workers."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic
from zipfile import ZipFile


SOURCE = Path(__file__).resolve().parent
PACKAGE = SOURCE.parent
ARMS = ('vanilla', 'target_caption', 'random_caption_swap')


def run(output, snapshot, report_python):
    start = monotonic()
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ZipFile(PACKAGE / 'replication_inputs.zip') as bundle:
            bundle.extractall(PACKAGE / 'inputs')
        sample = json.loads((PACKAGE / 'inputs/sample.json').read_text())

        def run_arm(arm):
            work = output / arm
            work.mkdir(exist_ok=True)
            print(f'Started {arm}', flush=True)
            command = [sys.executable, '-u', str(SOURCE / 'local_hits.py'), '--snapshot',
                       str(snapshot), '--output', str(work), '--limit', str(len(sample['tasks'])),
                       '--arms', arm]
            with (work / 'run.log').open('a') as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            print(f'{arm} exited with status {result.returncode}', flush=True)
            return arm, result.returncode

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = dict(pool.map(run_arm, ARMS))
        if any(codes.values()):
            raise SystemExit(f'Worker exit codes: {codes}. See the per-arm logs.')
        (output / 'timing.json').write_text(json.dumps(dict(
            wall_seconds=monotonic() - start, exit_codes=codes), indent=2) + '\n')
        if report_python:
            subprocess.run([str(report_python), str(SOURCE / 'report_hits.py'), str(output)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=PACKAGE / 'results')
    parser.add_argument('--report-python', type=Path)
    args = parser.parse_args()
    run(args.output.resolve(), args.snapshot.resolve(), args.report_python)
