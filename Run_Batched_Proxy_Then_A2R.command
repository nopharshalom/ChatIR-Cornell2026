#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
run_stage() {
    local script="$1" output="$2" status
    shift 2
    if [[ "${1:-}" == "--check-only" ]]; then
        .venv/bin/python -u "scripts/$script" "$@"
        return
    fi
    mkdir -p "$output"
    if /usr/bin/caffeinate -i .venv/bin/python -u "scripts/$script" "$@" 2>&1 | tee -a "$output/run.log"; then
        return 0
    else
        status=$?
    fi
    if [[ "$status" != 75 ]]; then return "$status"; fi
    echo 'GPU timeout; resuming this stage once.' | tee -a "$output/run.log"
    /usr/bin/caffeinate -i .venv/bin/python -u "scripts/$script" "$@" 2>&1 | tee -a "$output/run.log"
}
run_stage replay_batched_proxy.py ../output/batched-proxy-replay "$@"
run_stage run_a2r_matched400.py ../output/a2r-matched-400 "$@"
