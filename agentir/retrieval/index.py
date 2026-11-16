"""The cached corpus: 50k image embeddings + embeddings of their captions.

Image embeddings are computed once (see `agentir build-index`). Caption embeddings are
also cached because PlugIR's retrieval-context extraction re-encodes up to 500
candidate captions per round; precomputing them gives identical results much faster.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import torch
from tqdm import tqdm

log = logging.getLogger(__name__)


@dataclass
class CorpusIndex:
    paths: list[str]  # relative paths, e.g. "unlabeled2017/000000185565.jpg"
    image_emb: torch.Tensor  # [N, d] float32, L2-normalised
    captions: list[str] = field(default_factory=list)  # one caption per image ("" if unknown)
    caption_emb: Optional[torch.Tensor] = None  # [N, d]
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.path_to_idx = {p: i for i, p in enumerate(self.paths)}

    def __len__(self) -> int:
        return len(self.paths)

    def index_of(self, path: str) -> int:
        return self.path_to_idx[path]

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save({"paths": self.paths, "image_emb": self.image_emb.half().cpu(),
                    "captions": self.captions,
                    "caption_emb": None if self.caption_emb is None else self.caption_emb.half().cpu(),
                    "meta": self.meta}, path)

    @classmethod
    def load(cls, path: str | Path, device: str = "cpu") -> "CorpusIndex":
        d = torch.load(path, map_location="cpu", weights_only=False)
        if isinstance(d, (tuple, list)):  # PlugIR/ChatIR cache format: (ids, vectors)
            raise ValueError("This is a PlugIR-format cache; load it with CorpusIndex.from_plugir_cache")
        cap = d.get("caption_emb")
        return cls(paths=d["paths"], image_emb=d["image_emb"].float().to(device),
                   captions=d.get("captions") or [""] * len(d["paths"]),
                   caption_emb=None if cap is None else cap.float().to(device), meta=d.get("meta", {}))

    @classmethod
    def from_plugir_cache(cls, cache_path: str, search_space: list[str], device: str = "cpu") -> "CorpusIndex":
        ids, vecs = torch.load(cache_path, map_location="cpu")
        ids = ids.cpu()
        if len(ids) != len(search_space):
            raise ValueError(f"{cache_path} has {len(ids)} vectors, search space has {len(search_space)}")
        order = torch.argsort(ids)
        return cls(paths=list(search_space), image_emb=vecs.cpu()[order].float().to(device),
                   captions=[""] * len(search_space))


def load_search_space(path: str) -> list[str]:
    with open(path) as f:
        return json.load(f)


def load_captions(path: str, search_space: list[str]) -> list[str]:
    """visdial_captions.json is a list of {"id": path, "caption": [str]} aligned to the search space."""
    with open(path) as f:
        raw = json.load(f)
    by_id = {}
    for r in raw:
        cap = r["caption"]
        by_id[r["id"]] = cap[0] if isinstance(cap, list) else cap
    return [by_id.get(p, "") for p in search_space]


@torch.no_grad()
def encode_captions(encoder, captions: list[str], batch: int = 256) -> torch.Tensor:
    out = torch.zeros(len(captions), encoder.dim)
    for s in tqdm(range(0, len(captions), batch), desc="captions"):
        chunk = [c or "an image" for c in captions[s:s + batch]]
        out[s:s + len(chunk)] = encoder.encode_text(chunk).cpu()
    return out


def with_caption_embeddings(index: CorpusIndex, encoder, captions: list[str]) -> CorpusIndex:
    index.image_emb = torch.nn.functional.normalize(index.image_emb.float().cpu(), dim=-1)
    index.captions = list(captions)
    index.caption_emb = encode_captions(encoder, captions)
    index.meta["source"] = "plugir_cache"
    return index


class _ImageDataset(torch.utils.data.Dataset):
    def __init__(self, root: str, paths: list[str], encoder):
        self.root, self.paths, self.encoder = root, paths, encoder

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, i):
        return i, self.encoder.load_image(os.path.join(self.root, self.paths[i]))


@torch.no_grad()
def build_index(encoder, corpus_dir: str, search_space: list[str], captions: list[str],
                batch_size: int = 64, num_workers: int = 4) -> CorpusIndex:
    missing = [p for p in search_space if not os.path.exists(os.path.join(corpus_dir, p))]
    if missing:
        raise FileNotFoundError(f"{len(missing)} corpus images missing under {corpus_dir} "
                                f"(first: {missing[0]}). Run `agentir prepare-data` first.")
    ds = _ImageDataset(corpus_dir, search_space, encoder)
    dl = torch.utils.data.DataLoader(ds, batch_size=batch_size, num_workers=num_workers, shuffle=False)
    img_emb = torch.zeros(len(search_space), encoder.dim)
    for idx, pixels in tqdm(dl, desc="images"):
        img_emb[idx] = encoder.encode_images(pixels).cpu()

    cap_emb = encode_captions(encoder, captions)
    return CorpusIndex(paths=list(search_space), image_emb=img_emb, captions=list(captions),
                       caption_emb=cap_emb, meta={"encoder": type(encoder).__name__})
