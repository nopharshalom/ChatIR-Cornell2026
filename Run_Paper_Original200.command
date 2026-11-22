#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/paper-original200-mmr85-and-caption"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_paper_original200_pair.py "$@" 2>&1 | tee -a "$out/run.log"
