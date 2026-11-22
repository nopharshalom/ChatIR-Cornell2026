#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/agentir-visual-mmr85-200"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_visual_mmr85.py 2>&1 | tee -a "$out/run.log"
