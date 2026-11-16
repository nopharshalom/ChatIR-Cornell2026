# PlugIR · Guaranteed target-caption exposure

Open `PlugIR_Target_Caption_Hits10.ipynb` for the setup, inference, and plotting cells. The 200 matched vanilla records and sample manifest are included. New checkpoints go under `results/new_runs/`. Final plots, per-round Hits@10, and per-dialogue target exposure are under `results/figures/`.

The questioner gets the target's stored caption alongside the regular representative captions, replacing a seeded random slot if the target is missing. The existing prompt frames the supplied captions as alternatives, with no indication of which caption belongs to the target.

## Reproduce on Apple Silicon

1. Use Python 3.11. In a checkout of the team repository, create `.venv` and install `requirements-blip.txt` into it. Create a second virtual environment and install `requirements-mlx.txt` there. Point `REPO` and `MLX_ENV` in the notebook at those locations if needed.
2. Cache the two model snapshots under the repository's `cache/huggingface/hub` directory. With `huggingface_hub.snapshot_download`, pass this directory as `cache_dir` and use the model IDs and revisions below.

   | Model | Revision |
   | --- | --- |
   | `Salesforce/blip-itm-large-coco` | `19502f1e215844f7e48bd48473f86932486d3441` |
   | `mlx-community/Qwen3.5-9B-4bit` | `8b2b98c00a6b4d291155e4890773ca8f769aee53` |

3. The bundled ZIP supplies the cached corpus embeddings, corpus IDs and captions, and sampled target images. Its SHA-256 is `c1d54e0093e1dca14049431d74597e162e7d8502e4e2296750413c9d5b122c01`. Run the notebook's inference cell once both environments and checkpoints are ready. Set `RESULTS` to a fresh directory for an independent replication.
