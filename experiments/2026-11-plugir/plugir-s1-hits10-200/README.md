# PlugIR baseline vs. S1

Both arms use 200 fixed dialogue starts sampled with seed `20261111` from 1,786 eligible records. `sample.json` records the sample, eligibility exclusions, and the initial descriptions. Each arm searches the same 50,000-image corpus and stops at the first user-facing rank of 10 or better, with a maximum of 10 questions.

Vanilla PlugIR ranks with the latest reconstructed query. S1 ranks with both the initial and latest query and adds `1 / (60 + rank)` from each unique query. The fused order supplies both the user-facing results and the candidate pool. Both arms retain PlugIR's cosine k-means and entropy-based representative selection, the 500-to-50 candidate schedule, and the same Qwen question, answer, and rewrite configuration. This baseline uses the team's local Qwen setup for PlugIR's dialogue loop.

## Inspect the run

- Open `PlugIR_Baseline_and_S1_Hits10.ipynb` from this directory. The progress and plot cells use the saved JSON files. Plotting requires Matplotlib.
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
