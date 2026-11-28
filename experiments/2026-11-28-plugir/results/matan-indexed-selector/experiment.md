# Indexed visual question selection

## Decision

Keep captions-only minimum KL for the 400-dialogue comparison. The frozen fallback-only selector gained one target on tuning and zero on confirmation. Its benefit in both samples was earlier retrieval of some targets. Under the pre-confirmation decision rule in `fallback/freeze.json`, the final hit-rate gain did not replicate. The full 400 was not launched.

| Arm and sample | Final hits, visual / KL | Gained / lost | Questions changed / asked | User questions, visual / KL | Dialogue minutes, visual estimate / saved KL | Actual cached run, dialogue minutes |
|---|---:|---:|---:|---:|---:|---:|
| Comparable-score, tuning | 13 / 14 | 0 / 1 | 3 / 120 | 120 / 114 | 30.75 / 25.94 | 8.23 |
| Fallback-only, tuning | 15 / 14 | 1 / 0 | 11 / 100 | 100 / 114 | 27.09 / 25.94 | 17.27 |
| Frozen fallback-only, confirmation | 14 / 14 | 0 / 0 | 7 / 96 | 96 / 101 | 22.60 / 22.52 | 9.31 |

Each sample has 20 dialogues. The two tuning rows share the same controls. Unchanged complete trajectories were reused on 17/20, 13/20 and 15/20 cases respectively. Each changed trajectory was run live end-to-end. The from-scratch estimate includes 215, 85 and 78 seconds of visual inference respectively. Throughput varied during tuning. The runtimes compare measured and estimated dialogue work on different occasions, so the four-second confirmation difference is too small to interpret.

| Cumulative Hits@10 count | R0 | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Tuning KL | 0 | 4 | 7 | 8 | 10 | 10 | 11 | 12 | 12 | 12 | 14 |
| Tuning comparable-score | 0 | 4 | 7 | 8 | 9 | 9 | 10 | 11 | 11 | 11 | 13 |
| Tuning fallback-only | 0 | 6 | 8 | 9 | 11 | 11 | 13 | 14 | 14 | 14 | 15 |
| Confirmation KL | 0 | 6 | 7 | 9 | 11 | 12 | 13 | 13 | 14 | 14 | 14 |
| Confirmation fallback-only | 0 | 7 | 7 | 10 | 12 | 13 | 13 | 14 | 14 | 14 | 14 |

**Tuning.** The fallback-only rule gained dialogue 1906 at round six. From a common rank of 17, asking about snowboard clothing elicited “red and green” and moved the target to rank 1. KL elicited “trees” and finished at 18. Dialogue 742 improved from round 10 to 1 after a question about the man's shirt. Dialogue 1348 improved from round 2 to 1 after a question about kite color. Among the 14 shared hits, mean first-hit round fell from 3.86 to 3.14. No shared hit was slower.

**Confirmation.** The same 14 targets were retrieved by both arms. Mean first-hit round among them fell from 2.93 to 2.57. Dialogues 1969, 701 and 1091 improved from rounds 2 to 1, 8 to 7 and 6 to 3 respectively. The other 17 first-hit outcomes matched. Of the six KL misses, four had zero predicted differing pairs on every supported challenger scored at a fallback. In 2057 and 675 a question changed, and the targets finished at ranks 54 and 314. Thus the visual evidence had limited opportunity to recover the remaining targets in this sample. The confirmation set was used once with the frozen selector.

The original 400 saved KL controls have 290 initial hits and 376 hits by the budget. Only 110 dialogues ever ask a question under the matched stopping rule. The pilots deliberately concentrate on initial misses. Scaling their percentage-point changes across all 400 would overstate their impact.

## Reproduce or resume

Run from the AgentIR Project directory on the Mac with Metal access. The saved grid audit and the dialogue launcher both skip complete records. The tuning launcher is capped at 20 dialogues. It uses the existing BLIP environment, project MLX-VLM worker, cached corpus vectors and offline model snapshot.

```sh
ChatIR-Cornell2026/.venv-qwen/bin/python -c 'import mlx.core as mx; assert mx.metal.is_available(); mx.set_default_device(mx.gpu); print(mx.sum(mx.array([1, 2])).item())'
(cd ChatIR-Cornell2026 && /usr/bin/caffeinate -i .venv/bin/python -u scripts/check_indexed_visual_answers.py)
ChatIR-Cornell2026/Run_Indexed_Visual_Pilot.command tune
python3 ChatIR-Cornell2026/scripts/inspect_indexed_pilot.py output/matan-indexed-selector/v1/tune
```

## Grid audit

The unindexed lists for couch count, TV presence, and bathroom window presence contained nine entries for ten tiles. Their alignment is unknowable. Only those three cases were rerun, with the original question-specific instruction and an explicit object keyed 0–9. The train answer was already keyed and was recovered by parsing the saved response. The shirt answer was a complete ordered list and was reused from its scored record. Duplicate keys, missing keys and unsupported labels fail validation. Source records and reference labels remain in their original output folders. The five audit records and numbered grids are in the sibling folder `matan-indexed-grid-audit`.

| Question | Correct / clear tiles | Detected / truly different pairs | False splits / truly equal pairs |
|---|---:|---:|---:|
| Couch count | 6/7 | 0/0 | 6/21 |
| Television present | 9/9 | 20/20 | 0/16 |
| Bathroom window present | 9/9 | 18/18 | 0/18 |
| Train location, saved | 7/8 | 16/19 | 3/9 |
| Shirt color, saved | 10/10 | 27/27 | 0/18 |

A single miscount on tile 9 created all six false count differences. Blinded reference labels marked ambiguous tiles as null. Each pair denominator uses only tiles with a reference. Pairs within a grid share tiles.

## Dialogue protocol

V1 keeps all five caption proposals and computes the original minimum-KL choice. When that choice and at least one other distinct proposal have a supported answer vocabulary, Qwen estimates one answer per candidate for each supported proposal, using the numbered 2 × 5 grid. The supported families are single dominant color, yes/no, and train location. Counts are excluded following the couch result. A malformed KL row preserves KL. Unknown tiles contribute no different pairs. The selector maximizes the number of candidate pairs with two distinct known answers, preserving KL on ties. The grid model sees only the candidates and the question.

The target answers one selected question each round. Both arms use current-query MMR 0.85, the same 50,000 images, the same five-proposal generator, the archived answer/rewrite path, and a shrinking 500-to-50 candidate pool. Stop on first rank at most ten or after ten questions. Both control cohorts are resolved by `run_fixed500_mmr85.cohorts()`.

The tuning sample matches the earlier 20-dialogue visual-selection pilot exactly. The disjoint confirmation sample was reserved before inference. It excludes the tuning set, the prior 20 challenge cases, 10 repair cases and five manual examples. Only five untreated KL hits in rounds 4–10 were available. Confirmation therefore reserves nine early hits, five late hits and six misses, versus eight, six and six in tuning. The two sets have identical final KL hit counts. These are difficulty-stratified, initial-miss pilots. Their raw percentage changes cannot be extrapolated directly to the 400.

The runner reads archived complete trajectories up to the first decision change. An entirely unchanged trajectory is reused. A changed dialogue is run end-to-end from its original seed. On the common path the runner checks query, candidate IDs, all five proposals, KL choice and ranks. It also checks answer and rewrite for each unchanged round. Grid responses, raw model text and inference time are cached by ordered image IDs, prompt, question, model and token budget. Look up the filenames in each review under `answers`.

`v1/tune/plan.json` records both ID sets, runner hashes, model revision, budget and control record hashes. `v1/tune/indexed/dialogues/` contains complete trajectories. `summary.json` stores hit curves at rounds 0–10, each target's first-hit round, gains, losses and runtime. A null first-hit round means a miss at the budget. Actual dialogue wall time uses cache hits. The from-scratch timing estimate adds the recorded grid inference times to shared-pipeline time (archived for unchanged dialogues, measured live for branches). It excludes model loading and grid construction. The KL comparison is the summed per-dialogue time from the saved controls.

## Tuning decisions

The comparable-score rule finished at 13/20 versus 14/20. The only loss was dialogue 1894. Its first change at round one selected couch color (25 predicted splits) over floor color (zero). The target answered "brown". KL received "wood" and later elicited a red chair, reaching rank 7 on round four. The visual trajectory reached rank 44 after the first answer and finished at 40. Dialogue 1348 changed from beach color to kite color in round two and hit on the same round. Dialogue 1743 switched to a wall color already stated in the query and still missed. See `v1/tune/divergences.json` for questions, target answers, rewrites, ranks and grid predictions at each first divergence.

The comparable-score rule produced 52 usable rows from 75 attempted rows. Eleven rejected rows had ten keys but included the out-of-vocabulary color *beige*, nine returned a single object field, and three failed JSON decoding. Missing or invalid answers were never positionally imputed. The production vocabulary prompt for TV presence missed tile 6 on the same grid the calibration prompt scored perfectly. Accuracy on these five calibration questions may differ from accuracy on generated proposals.

The earlier multi-question group selector had sped up dialogue 1348 by replacing a generic "other object" question with kite color on turn one. On the tuning controls the generic fallback elicited "chandelier" four times in 297, "zebra" four times in 332 and "traffic light" eight times in 1579. The next variant changes only which KL decisions admit a visual override. It preserves specific KL choices and considers a positive visual split only when KL chose its explicit generic fallback. It uses the same prompts, vocabularies, pair count, cache and dialogue engine. A generic question's true split count is unmeasured.

```sh
ChatIR-Cornell2026/Run_Indexed_Visual_Pilot.command --fallback tune
python3 ChatIR-Cornell2026/scripts/inspect_indexed_pilot.py output/matan-indexed-selector/fallback/tune
```

The confirmation records use the same model and source hashes as the frozen tuning run. To verify or resume the completed sample:

```sh
ChatIR-Cornell2026/Run_Indexed_Visual_Pilot.command --fallback confirm
python3 ChatIR-Cornell2026/scripts/inspect_indexed_pilot.py output/matan-indexed-selector/fallback/confirm
```

The paired first-hit round for every ID is in each `summary.json`. The first divergent turn of every changed dialogue is in each `divergences.json`. All 60 saved arm records passed checks on control identity, ordered grid IDs for cached predictions, five proposals, selected question, target answer, stop condition and reconstructed hit curves. The 20 confirmation IDs are disjoint from all 20 tuning IDs.
