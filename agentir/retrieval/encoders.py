"""Frozen text/image encoders sharing one embedding space.

BLIP (Salesforce/blip-itm-large-coco) is the retriever used by both ChatIR and PlugIR;
the projection code mirrors `BlipForRetrieval` in PlugIR's eval.py so embeddings match.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

import torch
import torch.nn.functional as F

log = logging.getLogger(__name__)


def resolve_device(device: str = "auto") -> str:
    if device != "auto":
        return device
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Encoder(Protocol):
    dim: int
    device: str

    def encode_text(self, texts: list[str]) -> torch.Tensor: ...  # [B, d], L2-normalised

    def load_image(self, path: str) -> torch.Tensor: ...  # preprocessed pixel tensor

    def encode_images(self, pixels: torch.Tensor) -> torch.Tensor: ...  # [B, d], L2-normalised


class BlipEncoder:
    def __init__(self, model_name: str = "Salesforce/blip-itm-large-coco", device: str = "auto",
                 finetuned_ckpt: str | None = None):
        from transformers import AutoProcessor, BlipForImageTextRetrieval

        self.device = resolve_device(device)
        self.processor = AutoProcessor.from_pretrained(model_name)
        model = BlipForImageTextRetrieval.from_pretrained(model_name)
        if finetuned_ckpt:
            state = torch.load(finetuned_ckpt, map_location="cpu")
            msg = model.load_state_dict(state.get("model", state), strict=False)
            log.info("loaded fine-tuned retriever %s: %s", finetuned_ckpt, msg)
        self.model = model.to(self.device).eval()
        self.dim = self.model.config.image_text_hidden_size

    @torch.no_grad()
    def encode_text(self, texts: list[str]) -> torch.Tensor:
        enc = self.processor(text=texts, padding=True, truncation=True, max_length=512,
                             return_tensors="pt").to(self.device)
        out = self.model.text_encoder(input_ids=enc["input_ids"], attention_mask=enc["attention_mask"],
                                      return_dict=True)
        return F.normalize(self.model.text_proj(out.last_hidden_state[:, 0, :]), dim=-1).float()

    def load_image(self, path: str) -> torch.Tensor:
        from PIL import Image

        return self.processor(images=Image.open(path).convert("RGB"), return_tensors="pt")["pixel_values"][0]

    @torch.no_grad()
    def encode_images(self, pixels: torch.Tensor) -> torch.Tensor:
        out = self.model.vision_model(pixel_values=pixels.to(self.device), return_dict=True)
        return F.normalize(self.model.vision_proj(out.last_hidden_state[:, 0, :]), dim=-1).float()


def _as_tensor(out) -> torch.Tensor:
    """transformers<5 returns a tensor from get_*_features; >=5 returns an output object."""
    if isinstance(out, torch.Tensor):
        return out
    return out.pooler_output


class ClipEncoder:
    def __init__(self, model_name: str = "openai/clip-vit-base-patch16", device: str = "auto"):
        from transformers import CLIPModel, CLIPProcessor

        self.device = resolve_device(device)
        self.processor = CLIPProcessor.from_pretrained(model_name)
        self.model = CLIPModel.from_pretrained(model_name).to(self.device).eval()
        self.dim = self.model.config.projection_dim

    @torch.no_grad()
    def encode_text(self, texts: list[str]) -> torch.Tensor:
        # NB: CLIP truncates at 77 tokens, which hurts long reformulated queries.
        enc = self.processor(text=texts, padding=True, truncation=True, return_tensors="pt").to(self.device)
        return F.normalize(_as_tensor(self.model.get_text_features(**enc)), dim=-1).float()

    def load_image(self, path: str) -> torch.Tensor:
        from PIL import Image

        return self.processor(images=Image.open(path).convert("RGB"), return_tensors="pt")["pixel_values"][0]

    @torch.no_grad()
    def encode_images(self, pixels: torch.Tensor) -> torch.Tensor:
        return F.normalize(_as_tensor(self.model.get_image_features(pixel_values=pixels.to(self.device))),
                           dim=-1).float()


class FakeEncoder:
    """Hashed bag-of-words encoder for tests and the offline smoke run (no downloads)."""

    def __init__(self, dim: int = 512, device: str = "cpu"):
        self.dim = dim
        self.device = device

    def _vec(self, text: str) -> torch.Tensor:
        v = torch.zeros(self.dim)
        for tok in re.findall(r"[a-z]+", text.lower()):
            if tok in _STOP:
                continue
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        if v.abs().sum() == 0:
            v[0] = 1.0
        return v

    def encode_text(self, texts: list[str]) -> torch.Tensor:
        return F.normalize(torch.stack([self._vec(t) for t in texts]), dim=-1)

    def load_image(self, path: str) -> torch.Tensor:  # pragma: no cover - fake corpus is built directly
        raise NotImplementedError("FakeEncoder has no images; use agentir.smoke.build_fake_world")

    def encode_images(self, pixels: torch.Tensor) -> torch.Tensor:  # pragma: no cover
        raise NotImplementedError


_STOP = {"a", "an", "the", "is", "are", "in", "on", "of", "at", "it", "there", "and", "with",
         "what", "where", "how", "many", "does", "do", "this", "that", "image", "picture", "photo"}


def build_encoder(cfg) -> Encoder:
    if cfg.backend == "blip":
        return BlipEncoder(cfg.model_name, cfg.device, cfg.finetuned_ckpt)
    if cfg.backend == "clip":
        return ClipEncoder(cfg.model_name, cfg.device)
    if cfg.backend == "fake":
        return FakeEncoder()
    raise ValueError(cfg.backend)
