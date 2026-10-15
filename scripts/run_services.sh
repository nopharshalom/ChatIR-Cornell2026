#!/usr/bin/env bash
# Start the user-sim and agent services, run an evaluation over HTTP, then stop them.
# Usage: scripts/run_services.sh configs/backends/vllm.yaml configs/experiments/plugir.yaml [more configs/overrides...]
set -euo pipefail
CFG=(-c configs/base.yaml)
for c in "$@"; do
  if [[ "$c" == *.yaml ]]; then CFG+=(-c "$c"); else CFG+=(--set "$c"); fi
done

agentir serve-user  "${CFG[@]}" --port 7002 > logs_user.txt  2>&1 & USER_PID=$!
agentir serve-agent "${CFG[@]}" --port 7001 > logs_agent.txt 2>&1 & AGENT_PID=$!
trap 'kill $USER_PID $AGENT_PID 2>/dev/null || true' EXIT

for url in http://localhost:7001/health http://localhost:7002/health; do
  until curl -sf "$url" > /dev/null; do sleep 2; done
done
agentir run "${CFG[@]}" --set mode=http
