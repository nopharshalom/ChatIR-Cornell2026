"""Text-to-image search over the cached corpus.

Two views exist on purpose:
  * `Retriever` - full access, used by the evaluator (it knows how to rank a target).
  * `SearchTool` - what the agent gets: search/similarity only, every call metered as a
    "probe". The agent never sees the target, and probe counts become a cost axis
    (rounds of user effort vs. autonomous queries) for the paper.
"""
from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from typing import Sequence

import torch

from .index import CorpusIndex


class Retriever:
    def __init__(self, encoder, index: CorpusIndex, text_cache_size: int = 200_000):
        self.encoder = encoder
        self.index = index
        self.device = index.image_emb.device
        self._cache: OrderedDict[str, torch.Tensor] = OrderedDict()
        self._cache_size = text_cache_size
        self._lock = threading.Lock()
        self._encode_lock = threading.Lock()  # HF models are not guaranteed thread-safe

    # ------------------------------------------------------------------ encoding
    def encode_text(self, texts: Sequence[str]) -> torch.Tensor:
        out: list[torch.Tensor | None] = [None] * len(texts)
        todo: list[int] = []
        with self._lock:
            for i, t in enumerate(texts):
                v = self._cache.get(t)
                if v is not None:
                    self._cache.move_to_end(t)
                    out[i] = v
                else:
                    todo.append(i)
        if todo:
            uniq = list(dict.fromkeys(texts[i] for i in todo))
            with self._encode_lock:
                embs = self.encoder.encode_text(uniq).to(self.device)
            new = dict(zip(uniq, embs))
            with self._lock:
                for t, v in new.items():
                    self._cache[t] = v
                    if len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
            for i in todo:
                out[i] = new[texts[i]]
        return torch.stack(out)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ search
    def similarity(self, texts: Sequence[str]) -> torch.Tensor:
        """Cosine similarity of each text to every corpus image: [B, N]."""
        return self.encode_text(texts) @ self.index.image_emb.T

    def search(self, text: str, k: int = 10) -> tuple[torch.Tensor, torch.Tensor]:
        sims = self.similarity([text])[0]
        top = torch.topk(sims, k)
        return top.indices, top.values

    def ranks_of(self, texts: Sequence[str], target_idx: int) -> list[int]:
        """0-based rank of the target for each query (0 == top-1), as in ChatIR/PlugIR eval."""
        sims = self.similarity(texts)
        tgt = sims[:, target_idx : target_idx + 1]
        return (sims > tgt).sum(dim=1).tolist()


class SearchTool:
    """The agent's metered window onto the corpus (no target access)."""

    def __init__(self, retriever: Retriever, counter: Counter | None = None):
        self._r = retriever
        self.index = retriever.index
        self.counter = counter if counter is not None else Counter()

    def with_counter(self, counter: Counter) -> "SearchTool":
        return SearchTool(self._r, counter)

    def similarity(self, texts: Sequence[str], purpose: str = "probe") -> torch.Tensor:
        self.counter[f"search:{purpose}"] += len(texts)
        return self._r.similarity(texts)

    def search(self, text: str, k: int = 10, purpose: str = "probe"):
        self.counter[f"search:{purpose}"] += 1
        return self._r.search(text, k)

    def encode_text(self, texts: Sequence[str]) -> torch.Tensor:
        return self._r.encode_text(texts)
