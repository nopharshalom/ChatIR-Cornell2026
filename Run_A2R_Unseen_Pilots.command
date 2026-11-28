#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out=../output/a2r-unseen-paired-pilots
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_a2r_unseen_pilots.py "$@" 2>&1 | tee -a "$out/run.log"
