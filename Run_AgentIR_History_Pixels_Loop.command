#!/bin/bash
set -euo pipefail

repo_dir=$(cd "$(dirname "$0")" && pwd -P)
project_dir=$(dirname "$repo_dir")
cd "$project_dir"
bundled_cli=/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
if [[ -x "$bundled_cli" ]]; then
    codex_bin=$bundled_cli
else
    codex_bin=$(command -v codex) || { echo 'Codex CLI could not be located.' >&2; exit 1; }
fi
for input in output/history-pixels-loop/{split.json,controls/{vanilla,mmr85}.json}; do
    [[ -s "$input" ]] || { echo "Missing experiment input: $input" >&2; exit 1; }
done

prompt=$(cat "$repo_dir/scripts/history_pixels_loop.md")

case "${1:-}" in
    --check-only)
        "$codex_bin" --version
        echo "Found the saved split and both replay controls."
        exit 0 ;;
    "") ;;
    *) echo "Usage: $0 [--check-only]" >&2; exit 2 ;;
esac
exec /usr/bin/caffeinate -i "$codex_bin" -C "$project_dir" --sandbox workspace-write --ask-for-approval on-request "$prompt"
