#!/bin/bash
set -eu
set -o pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/agentir-mmr-history-visual-200"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_mmr_history_visual_200.py 2>&1 | tee -a "$out/overnight.log"
