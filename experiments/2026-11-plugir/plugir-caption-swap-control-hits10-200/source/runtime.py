"""Device selection and local model-cache defaults for inference scripts."""

import os
from pathlib import Path

import torch

os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parent / "cache" / "huggingface"))


def resolve_device(requested="auto"):
    if requested == "auto":
        if torch.cuda.is_available():
            return "cuda:0"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    device = torch.device(requested)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA is unavailable. Use --device mps or --device cpu.")
    if device.type == "mps" and not torch.backends.mps.is_available():
        raise ValueError("Apple GPU is unavailable. Use native arm64 Python or --device cpu.")
    if device.type not in {"cuda", "mps", "cpu"}:
        raise ValueError("Supported devices: auto, cpu, mps, cuda, cuda:0, ...")
    return str(device)
