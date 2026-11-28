#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
/usr/bin/caffeinate -i .venv/bin/python -u scripts/replay_matan_display_mmr.py --finish
