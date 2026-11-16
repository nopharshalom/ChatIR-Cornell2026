# PlugIR · Fresh-sample caption replication

The same three methods run on 200 new dialogues. Seed 20261114 samples from the 1,586 eligible records left after excluding the original 200. The sample manifest records IDs, starting descriptions, and the source dataset hash. All arms use the same sample, model checkpoints, and first-hit stopping rule with a two-question cap. The primary measure is Hits@10 after one question. Hits@10 after two questions is the follow-up.

Open `PlugIR_Fresh_Sample_Replication.ipynb` from this folder. The notebook launches or resumes the arms in two concurrent workers. It checks counts and plots the completed matched comparison. The packaged input archive contains the sample’s target images, captions, and corpus index. The launcher extracts it to `inputs/`, which is generated locally. The `results/` directory contains each arm’s checkpoints and the final figure and CSV.

## Reproduce on Apple Silicon

1. Use Python 3.11. In a checkout of the team repository, create `.venv` and install `requirements-blip.txt` into it. Create a second virtual environment and install `requirements-mlx.txt` there. Point `REPO` and `MLX_ENV` in the notebook at those locations if needed.
2. Cache the two model snapshots under the repository's `cache/huggingface/hub` directory. With `huggingface_hub.snapshot_download`, pass this directory as `cache_dir` and use the model IDs and revisions below.

   | Model | Revision |
   | --- | --- |
   | `Salesforce/blip-itm-large-coco` | `19502f1e215844f7e48bd48473f86932486d3441` |
   | `mlx-community/Qwen3.5-9B-4bit` | `8b2b98c00a6b4d291155e4890773ca8f769aee53` |

