#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/agentir-mmr85-all-round-caption-200"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_mmr85_all_round_caption.py 2>&1 | tee -a "$out/run.log"
