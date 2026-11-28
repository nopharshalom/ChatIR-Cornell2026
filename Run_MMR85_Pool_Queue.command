#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
./Run_MMR85_Fixed500.command --wait
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/mmr85-s2-history-400"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_fixed500_mmr85.py --s2-history --wait 2>&1 | tee -a "$out/run.log"
