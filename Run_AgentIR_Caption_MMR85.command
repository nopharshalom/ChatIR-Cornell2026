#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
out="../output/agentir-caption-probe-mmr85-200"
mkdir -p "$out"
echo 'Waiting for the current visual run to release the GPU...'
.venv/bin/python -c 'import fcntl; f=open("../output/agentir-visual-mmr85-200/run.lock"); fcntl.flock(f,fcntl.LOCK_SH)'
/usr/bin/caffeinate -i .venv/bin/python -u scripts/run_caption_probe_mmr85.py 2>&1 | tee -a "$out/run.log"
