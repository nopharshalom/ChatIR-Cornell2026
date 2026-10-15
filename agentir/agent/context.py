"""PlugIR retrieval-context extraction (paper Alg. 1 / `get_related_captions`).

Take the top-n candidates for the current query (n shrinks linearly with the round),
cluster their image embeddings with cosine k-means, and from each cluster keep the
caption whose caption-to-candidates similarity distribution has the lowest entropy.
"""
from __future__ import annotations

import torch

from ..retrieval import SearchTool
from .state import DialogState


def spherical_kmeans(x: torch.Tensor, k: int, iters: int = 100, seed: int = 0, tol: float = 1e-4) -> torch.Tensor:
    """Cosine k-means. Stand-in for fast_pytorch_kmeans.KMeans(mode='cosine') used by PlugIR."""
    x = torch.nn.functional.normalize(x.float(), dim=-1)
    k = min(k, x.shape[0])
    g = torch.Generator(device="cpu").manual_seed(seed)
    centers = x[torch.randperm(x.shape[0], generator=g)[:k].to(x.device)].clone()
    labels = torch.zeros(x.shape[0], dtype=torch.long, device=x.device)
    for _ in range(iters):
        labels = (x @ centers.T).argmax(dim=1)
        new = torch.zeros_like(centers).index_add_(0, labels, x)
        empty = new.norm(dim=1) == 0
        new[empty] = centers[empty]
        new = torch.nn.functional.normalize(new, dim=-1)
        shift = (new - centers).norm(dim=1).max()
        centers = new
        if shift < tol:
            break
    return labels


def candidate_set_size(thres_low: int, round_: int) -> int:
    """PlugIR: int(thres_low - (round-1) * thres_low/10) -> 500, 450, ..., 50."""
    return max(1, int(thres_low - (round_ - 1) * (thres_low / 10.0)))


def related_captions(state: DialogState, search: SearchTool, thres_low: int = 500,
                     n_clusters: int = 10, seed: int = 0) -> tuple[list[str], torch.Tensor]:
    """Returns (representative captions, top-n candidate indices)."""
    idx = search.index
    if idx.caption_emb is None:
        raise ValueError("index has no caption embeddings; rebuild it with `agentir build-index`")
    n = min(candidate_set_size(thres_low, state.round), len(idx))
    sims = search.similarity([state.query], purpose="context")[0]
    topk = torch.topk(sims, n).indices  # descending (PlugIR uses ascending argsort; order is irrelevant)
    img = idx.image_emb[topk]
    cap = idx.caption_emb[topk]
    p = torch.softmax(cap @ img.T, dim=1)
    entropy = -(p * p.clamp_min(1e-12).log()).sum(dim=1)
    order = torch.argsort(entropy)  # lowest entropy first

    labels = spherical_kmeans(img, n_clusters, seed=seed)
    labels_sorted = labels[order]
    caps: list[str] = []
    for c in range(n_clusters):
        hit = (labels_sorted == c).nonzero()
        if len(hit):
            caps.append(idx.captions[topk[order[hit.min()]].item()])
    return caps, topk


def format_candidates(caps: list[str]) -> str:
    return "".join(f"{i}. {c}\n" for i, c in enumerate(caps))
