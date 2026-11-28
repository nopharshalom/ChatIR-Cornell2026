"""Run the standard image worker, isolating the temporary candidate-answer RNG stream."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import matan_grid_worker


def requests(stream):
    import mlx.core as mx

    state = None
    for line in stream:
        op = json.loads(line).get('proxy_rng')
        if op is None:
            yield line
            continue
        if op == 'save' and state is None:
            state = [mx.array(x) for x in mx.random.state]
            mx.eval(*state)
        elif op == 'restore' and state is not None:
            for i, value in enumerate(state):
                mx.random.state[i][:] = value
            mx.eval(*mx.random.state)
            state = None
        else:
            raise ValueError('Unexpected proxy RNG operation')
        print(json.dumps({'proxy_rng': op}), flush=True)


if __name__ == '__main__':
    with patch.object(sys, 'stdin', requests(sys.stdin)):
        matan_grid_worker.worker(Path(sys.argv[2]))
