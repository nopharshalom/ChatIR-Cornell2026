# Novelty and grounded-rewrite ablations

Two separate end-to-end variants on the original 200 dialogues, compared with the existing local vanilla records. Queue order: novelty, then grounded_rewrite. Frozen BLIP, shrinking pool, cluster representatives, Qwen snapshot, target-image simulator, initial sample and ten-question/first-hit stopping remain the same.

- **Novelty:** original candidate generation, followed by one Qwen judgement of whether each question adds unresolved information. Exact previous questions are also excluded. Original KL selects among accepted questions. If none qualifies, generate up to two batches of three questions and check novelty again. After unsuccessful regeneration, use the original candidates/selector and log the fallback. Original rewriting and paraphrasing remain unchanged. This tests a novelty filter plus conditional regeneration, not a new information-gain score.
- **Grounded rewrite:** original generation and KL selection, with a new single-call rewrite prompt using the initial caption and full actual Q/A history. It distinguishes question premises from user-confirmed facts, preserves entity relations, handles uncertainty/denial and permits corrections to the initial caption. No target image or target rank enters rewriting. It retains a justified unchanged query instead of applying the baseline's forced paraphrase. Same 512 output-token ceiling as baseline. Invalid output falls back to the baseline rewrite and is logged.

No changes to probe selection are included. Neither arm adds VLM evidence inspection; the target-image answer simulator remains as in earlier runs.

## Launch or resume

From the project directory:

```bash
caffeinate -i ChatIR-Cornell2026/.venv/bin/python \
  ChatIR-Cornell2026/scripts/run_novelty_grounding.py
```

Run from macOS Terminal with Metal access. Both arms run sequentially. The command verifies the sample, baseline hashes and archived pipeline, pins its own source/prompts in plan.json and skips completed dialogues on resume. Do not edit the runner after starting the experiment; use a new output directory for changed configurations.

## Graceful stop before closing the laptop

In a second Terminal tab, from the same project directory:

```bash
ChatIR-Cornell2026/.venv/bin/python \
  ChatIR-Cornell2026/scripts/run_novelty_grounding.py --stop
```

This requests a stop after the current dialogue. Wait for STOPPED and the original terminal's shell prompt before closing the lid. A ten-question dialogue can take a few minutes. Launching the original command again clears the previous stop request and resumes.

For an immediate interruption, press Ctrl+C in the running terminal. Completed dialogues remain saved; the current incomplete dialogue restarts from its beginning on the next launch. Depending on when the signal reaches the model subprocess, a worker-exit traceback may appear; saved dialogue files remain valid.

## Results and timing

Output: `output/agentir-novelty-grounding-200/` in the project directory. Each arm has `dialogues/<qid>.json`; shared `summary.json` updates after every completed dialogue. Partial summaries compare only matching completed IDs with vanilla and are not full-sample estimates. `status.json` reports running/stopped/complete. Detailed novelty decisions, regenerations, raw new rewrite outputs and fallbacks are saved in the dialogue records.

The previous local vanilla 200 took about 75 minutes. Planning estimates, not measured timings for these new variants: novelty 85–115 minutes; grounded rewrite 70–100 minutes; roughly 2.5–3.5 hours combined, with a four-hour allowance if regeneration or longer dialogues add work. Travel/sleep time is additional. 138 of the original 200 start within the top ten and require no questions.

Validation: six CPU fixture tests cover semantic-judge parsing, repeated-question rejection, malformed-output fallback, isolation of the two ablations, first-hit stopping and checkpoint-preserving graceful stop/resume. Plan validation checks all 200 against saved vanilla. Live Qwen/MPS smoke validation must occur when launched from Terminal; it was unavailable in the assistant tool session.
