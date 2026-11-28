"""Interrupt the dialogue on a Metal timeout before PlugIR can retry a single request."""
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from run_matan_400 import mac


def stop_on_metal_timeout(error):
    if 'kIOGPUCommandBufferCallbackErrorTimeout' in str(error):
        print('Metal timed out. Discarding the unfinished dialogue.', file=sys.stderr, flush=True)
        # PlugIR catches Exception and resamples the request. Exit the runner instead
        # so the launcher restarts with the original dialogue seed and clean GPU state.
        raise SystemExit(75) from error


@contextmanager
def guard_metal_timeout():
    read = mac.MacQwen.read

    def read_response(worker):
        try:
            return read(worker)
        except RuntimeError as error:
            stop_on_metal_timeout(error)
            raise

    with patch.object(mac.MacQwen, 'read', read_response):
        try:
            yield
        except RuntimeError as error:
            # A timeout in the parent PyTorch process also needs a fresh process.
            stop_on_metal_timeout(error)
            raise


def main():
    with guard_metal_timeout():
        runpy.run_path(str(Path(__file__).with_name('run_history_veto.py')), run_name='__main__')


if __name__ == '__main__':
    main()
