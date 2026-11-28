#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
exec /usr/bin/caffeinate -i .venv/bin/python -u scripts/run_proxy_retrieval.py "$@"
