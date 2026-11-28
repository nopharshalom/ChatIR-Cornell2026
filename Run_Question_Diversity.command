#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out=../output/mmr-within-round-questions
mkdir -p "$out"
run_pilot() {
    /usr/bin/caffeinate -i .venv/bin/python -u scripts/run_question_diversity.py "$@" 2>&1 | tee -a "$out/run.log"
}

if run_pilot "$@"; then
    exit 0
else
    status=$?
fi
if [[ "$status" != 75 ]]; then
    exit "$status"
fi
echo 'Restarting once from the last saved dialogue after the GPU timeout.' | tee -a "$out/run.log"
run_pilot "$@"
