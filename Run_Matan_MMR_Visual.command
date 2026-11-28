#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/matan-mmr-visual-400"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_matan_mmr_visual.py "$@" 2>&1 | tee -a "$out/run.log"
