# PlugIR S3 selector comparison

The comparison uses the same 200 dialogue starts and 50,000-image corpus as the baseline run. All methods use the latest query for the user-facing ranking and stop at the first top-10 hit or after 10 questions.

Current Top 10 picks the ten highest-ranked captions from the latest query. History MMR uses the fused ranking for relevance and subtracts a penalty for similarity to captions’ source images already selected. Both use the same current-query candidate pool as vanilla and Fused Top 10. Vanilla and Fused Top 10 are copied from the completed runs and included under `results/`.

Open `PlugIR_S3_Selector_Comparison.ipynb` from this directory. It can start or resume both new selectors concurrently, read progress, and generate the four-method Hits@10 plot and CSV. Each new selector has its own run log, configuration, and dialogue checkpoints. Completed figures are under `results/figures/`. Partial figures use only dialogue IDs completed in all four methods and are labeled with their sample size.

## Reproduce on Apple Silicon

1. Use Python 3.11. In a checkout of the team repository, create `.venv` and install `requirements-blip.txt` into it. Create a second virtual environment and install `requirements-mlx.txt` there. Point `REPO` and `MLX_ENV` in the notebook at those locations if needed.
2. Cache the two model snapshots under the repository's `cache/huggingface/hub` directory. With `huggingface_hub.snapshot_download`, pass this directory as `cache_dir` and use the model IDs and revisions below.

   | Model | Revision |
   | --- | --- |
   | `Salesforce/blip-itm-large-coco` | `19502f1e215844f7e48bd48473f86932486d3441` |
   | `mlx-community/Qwen3.5-9B-4bit` | `8b2b98c00a6b4d291155e4890773ca8f769aee53` |

3. The bundled ZIP supplies the cached corpus embeddings, corpus IDs and captions, and sampled target images. Its SHA-256 is `c1d54e0093e1dca14049431d74597e162e7d8502e4e2296750413c9d5b122c01`. Run the notebook's inference cell once both environments and checkpoints are ready. Set `RESULTS` to a fresh directory for an independent replication.
