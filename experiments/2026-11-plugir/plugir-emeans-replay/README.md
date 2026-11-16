# PlugIR e-means replay

This CPU test reuses the 305 saved vanilla candidate pools from the same 200 dialogues. The two clustering methods get the same pool and use the same lowest-entropy representative rule within each cluster. The target ID is used only for scoring. The replay makes no Qwen calls.

The vanilla comparator uses the shipped PlugIR cosine k-means implementation and the original per-turn seed. The e-means arm uses ten spherical clusters, the upstream defaults (five epochs and automatic batch size), the same seed value, and a nearest-centroid assignment after training. Clustering timings cover the respective fit and label operations on the same CPU with two PyTorch threads. They exclude caption encoding and entropy calculation.

The replay batches captions on CPU. The original run encoded captions individually using MPS. Both replay arms share the resulting entropy scores. The summary includes agreement with the saved vanilla representative sets to check the influence of this difference.

`rounds.csv` records each method’s selected image IDs, target exposure, and clustering latency. `summary.json` reports exposure conditional on the target being in the pool and counts the dialogues with any exposure. An exposure gain is evidence about the selector. End-to-end Hits@10 requires running the new questions, answers, and query updates.

## Reproduce

Use the PlugIR Mac environment and BLIP checkpoint documented in the target-caption experiment. Obtain the [official Faiss source at the pinned revision](https://github.com/facebookresearch/faiss/tree/ebab8f784c1d177129602269d5ad966c525fbdcb/contrib/e_means). Copy `base.py`, `e_means_torch.py`, `torch_backend.py`, and `e_means_spherical.py` to an `emeans_upstream` directory under a Python import path. Also add the target-caption experiment’s `source` directory to that path. Run `replay.py` with `--vanilla` pointing to the saved vanilla `dialogues` folder, `--inputs` pointing to the extracted experiment bundle, `--cache` pointing to the Hugging Face hub cache, and `--output` pointing to the results directory.
