#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/matan-repaired-images-pilot-10"
mkdir -p "$out"
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_matan_repaired_pilot.py "$@" 2>&1 | tee -a "$out/run.log"
