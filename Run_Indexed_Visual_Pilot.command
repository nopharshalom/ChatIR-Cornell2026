#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/matan-indexed-selector"
mkdir -p "$out"
runner="$out/v1/source/run_indexed_visual_pilot.py"
export PYTHONPATH="$PWD/scripts${PYTHONPATH:+:$PYTHONPATH}"
if [[ "${1:-}" == "--fallback" ]]; then
    runner="scripts/run_indexed_visual_pilot.py"
    shift
fi
/usr/bin/caffeinate -i .venv/bin/python -u "$runner" "$@" 2>&1 | tee -a "$out/run.log"
