# PlugIR baseline vs. S2 and S3 Fused Top 10

All three methods use the same 200 dialogue starts sampled with seed `20261111` from 1,786 eligible records. `sample.json` records the sample, eligibility exclusions, and initial descriptions. Each method searches the same 50,000-image corpus and stops when the target first reaches the user-facing top 10 or after 10 questions.

S2 uses reciprocal rank fusion across all distinct queries so far to fill the candidate pool, then runs PlugIR’s cosine k-means and entropy-based representative selection. S3 keeps the latest query’s candidate pool and selects its 10 highest history-ranked images using the same fusion rule. Both variants use the latest query for the user-facing ranking and keep the same 500-to-50 pool schedule and Qwen settings.

The completed vanilla records come from the matched S1 run. The notebook includes a copy of those checkpoints and `results/vanilla_config.json`. The report verifies that the baseline and variants share the same settings and source hashes and plots only dialogue IDs present in all three arms.

## Inspect the run

- Open `PlugIR_S2_and_S3_Fused_Top10.ipynb` from this directory. The progress and plot cells use the saved JSON files. Plotting requires Matplotlib.
- `results/config.json` records the actual settings and source hashes. Each method's `dialogues/` folder holds per-dialogue ranks, questions and answers, queries, evidence selections, and timing. `results/llm_requests.jsonl` records generation outputs and timing.
- The completed comparison goes in `results/figures/` as a PNG, round-level CSV, and matched dialogue IDs. Optional interim outputs go in `results/partial_figures/` and display their sample size.

## Reproduce on Apple Silicon

1. Use Python 3.11. In a checkout of the team repository, create `.venv` and install `requirements-blip.txt` into it. Create a second virtual environment and install `requirements-mlx.txt` there. Point `REPO` and `MLX_ENV` in the notebook at those locations if needed.
2. Cache the two model snapshots under the repository's `cache/huggingface/hub` directory. With `huggingface_hub.snapshot_download`, pass this directory as `cache_dir` and use the model IDs and revisions below.

   | Model | Revision |
   | --- | --- |
   | `Salesforce/blip-itm-large-coco` | `19502f1e215844f7e48bd48473f86932486d3441` |
   | `mlx-community/Qwen3.5-9B-4bit` | `8b2b98c00a6b4d291155e4890773ca8f769aee53` |

3. The bundled ZIP supplies the cached corpus embeddings, corpus IDs and captions, and sampled target images. Its SHA-256 is `c1d54e0093e1dca14049431d74597e162e7d8502e4e2296750413c9d5b122c01`. Run the notebook's inference cell once both environments and checkpoints are ready. Set `RESULTS` to a fresh directory for an independent replication.
