#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/matan-visual-question-selection-20"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_matan_visual_selection.py "$@" 2>&1 | tee -a "$out/run.log"
