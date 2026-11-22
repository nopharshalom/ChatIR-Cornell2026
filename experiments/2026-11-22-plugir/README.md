# PlugIR experiment archive, November 16–22

The results directory preserves the per-run JSON and CSV outputs from the local AgentIR workspace. Plans and dialogue IDs identify each sample. Some runs are pilots or intermediate runs. Check record counts before using a run as a completed evaluation.

## Four-method comparison on 400 dialogues

| Method | Paper’s original 200 | Additional 200 |
| --- | --- | --- |
| Vanilla PlugIR | Earlier archive, plugir-overnight-hits10-200/results/vanilla | agentir-overnight-200-300/vanilla |
| Current-query MMR 0.85 | paper-original200-mmr85-and-caption/current_mmr85 | agentir-mmr-history-visual-200/mmr85 |
| Agent probing every round (caption-based) | paper-original200-mmr85-and-caption/caption_all_rounds | agentir-vanilla-all-round-probes-200/caption_all_rounds |
| S2 all-query history RRF pool | Earlier archive, plugir-overnight-hits10-200/results/s2_history_pool | agentir-overnight-200-300/s2_history_pool |

Use the original sample.json in the earlier archive and the tasks in agentir-vanilla-all-round-probes-200/plan.json for the additional 200. Select dialogue records by these IDs, since some run directories contain more than 200 records. The two manifests contain 400 distinct dialogue IDs and target images. At each round count dialogues whose target has ranked in the top ten at least once through that round. The figure is [PlugIR_400_Agent_Probing.png](figures/PlugIR_400_Agent_Probing.png).

The earlier archive is under experiments/2026-11-plugir. Runners and launchers preserve the local workspace layout used for the experiments. In that layout ChatIR-Cornell2026, output, and tmp are siblings. For replay, place the archived result folders under output using their original directory names. Model weights, target image files, local environments, and runtime logs are separate from this archive.
