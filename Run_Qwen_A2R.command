#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out=../output/qwen-a2r-dpp
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_qwen_a2r.py "$@" 2>&1 | tee -a "$out/run.log"
