#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out=../output/matched-vanilla-mmr85
mkdir -p "$out"
run_queue() {
    /usr/bin/caffeinate -i .venv/bin/python -u scripts/run_matched_vanilla_mmr85.py "$@" 2>&1 | tee -a "$out/run.log"
}
if run_queue "$@"; then
    exit 0
else
    status=$?
fi
if [[ "$status" != 75 ]]; then
    exit "$status"
fi
echo 'Restarting from the last checkpoint after a GPU timeout.' | tee -a "$out/run.log"
run_queue "$@"
