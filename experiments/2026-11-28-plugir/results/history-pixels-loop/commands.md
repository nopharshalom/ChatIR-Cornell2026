# Run directly from the AgentIR Project directory

The interactive .command file opens another Codex session. The following commands invoke only the experiment runners. Run one inference job at a time on host Metal. A completed arm revalidates its plan and reads its per-dialogue checkpoints. The output lock prevents two pilot jobs writing at once.

```sh
ChatIR-Cornell2026/.venv-qwen/bin/python -c 'import mlx.core as mx; assert mx.metal.is_available(); mx.set_default_device(mx.gpu); print(mx.sum(mx.array([1,2])).item())'
/usr/bin/caffeinate -i ChatIR-Cornell2026/.venv/bin/python -u ChatIR-Cornell2026/scripts/run_history_pixels_pilot.py development ordinary unseen caption pixels
```

The launch uses the same archived MacQwen worker, MPS BLIP model, cached 50k image index, and cohort-specific target image folders as the prior MMR 0.85 pilots. Each live dialogue resets the generator with 1021 + qid and the shared PyTorch seed each round. Each question comes from the same five caption proposals and one target answer is requested per turn. The ordinary arm reproduces the archived control, with no-repeat filtering applied to each actual screen. A remaining miss after ten questions is encoded as first hit 11.

The first runner records screen IDs and evidence IDs separately, including the pre-swap screen and both the raw and filtered target rank. Only evaluation reads target ranks. The verifier receives the candidate evidence and the visual question. Its color output is compared to the user's answer by the policy. It never receives the target ID. Caption and image verifier entries are stored separately by model snapshot, image ID, question, normalized answer, exact prompt and token limit.

The original archived questions, target answers and rewrites are replayed only on a matching evidence/decision prefix. A new question or a need to extend past archived stopping causes a full rerun from that task's original stochastic seed. The grid question-selector follow-up can also reuse a complete prior policy-matched trajectory, ending earlier when the no-repeat screen hits. The actual time recorded includes candidate inference, any replay, and live regeneration. Times for replay-heavy arms exclude the shared dialogue work originally performed by the archived runs.

For paired analysis after two arms complete, use the project Python and supply the split and arm names, for example:

```sh
ChatIR-Cornell2026/.venv/bin/python ChatIR-Cornell2026/scripts/compare_history_pixels.py development unseen ordinary
```

The saved 400-control replay is sufficient. To reproduce only that archived computation, use the existing command:

```sh
ChatIR-Cornell2026/.venv/bin/python ChatIR-Cornell2026/scripts/replay_rejected_top10.py --sample-size 400
```

The evidence and question-selection follow-ups use these commands. The question runner copies prior compatible grid predictions into the new experiment's cache and records any reused trajectory's path and hash. The caption version uses the same tile numbering, eligibility rule, answer vocabulary and pair score with numbered captions substituted as evidence.

```sh
/usr/bin/caffeinate -i ChatIR-Cornell2026/.venv/bin/python -u ChatIR-Cornell2026/scripts/run_history_pixels_pilot.py development contrast
/usr/bin/caffeinate -i ChatIR-Cornell2026/.venv/bin/python -u ChatIR-Cornell2026/scripts/run_history_pixel_questions.py development q-pixels q-caption
```

Only the text-only fallback qualifier advanced. Its confirmation commands, in order, are:

```sh
/usr/bin/caffeinate -i ChatIR-Cornell2026/.venv/bin/python -u ChatIR-Cornell2026/scripts/run_history_pixels_pilot.py confirmation ordinary
/usr/bin/caffeinate -i ChatIR-Cornell2026/.venv/bin/python -u ChatIR-Cornell2026/scripts/run_history_pixel_questions.py confirmation q-caption
ChatIR-Cornell2026/.venv/bin/python ChatIR-Cornell2026/scripts/compare_history_pixels.py confirmation q-caption ordinary
ChatIR-Cornell2026/.venv/bin/python ChatIR-Cornell2026/scripts/verify_history_pixels.py
```
