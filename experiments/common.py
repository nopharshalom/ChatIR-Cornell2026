"""Shared helpers for the meeting experiments (E1-E3)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentir.metrics import compute_metrics  # noqa: E402

PLUGIR_RECONED = ("dialogues/ours_final_q_n_5_recall_hitting_10_thres_low_500_recon_true_referring_true_"
                  "filtering_true_select_true_reconed.json")


def load_retriever_from_args(args):
    """Real BLIP index from configs, or the synthetic world with --fake (for testing the scripts)."""
    if getattr(args, "fake", False):
        from agentir.retrieval import Retriever
        from agentir.smoke import build_fake_world

        enc, index, queries, lookup = build_fake_world(n_queries=args.limit or 30)
        return Retriever(enc, index), queries, lookup
    from agentir.config import load_config
    from agentir.runtime import load_retriever

    cfg = load_config(args.config, args.set)
    return load_retriever(cfg), None, None


def add_common_args(ap):
    ap.add_argument("-c", "--config", action="append", default=[],
                    help="agentir YAML configs, e.g. -c configs/base.yaml -c configs/backends/colab.yaml")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--limit", type=int, default=None, help="first N dialogs only")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--fake", action="store_true", help="synthetic world (no models), to test the plumbing")
    ap.add_argument("--out", required=True, help="output folder")


def plugir_pool_size(round_: int, thres_low: int = 500) -> int:
    """Candidates PlugIR shows the questioner before asking question `round_` (1-based)."""
    return max(1, int(thres_low - (round_ - 1) * (thres_low / 10.0)))


def dpp_greedy(img: torch.Tensor, scores: torch.Tensor, k: int, alpha: float = 10.0) -> list[int]:
    """Greedy MAP for a quality x diversity DPP (A2R's DCP).

    L = diag(q) S diag(q), S = cosine similarity of image embeddings, q = exp(alpha * score).
    Uses the incremental Cholesky update of Chen et al. (2018), O(n k^2).
    """
    n = img.shape[0]
    k = min(k, n)
    x = torch.nn.functional.normalize(img.float().cpu(), dim=-1)  # tiny (<= 50 x 50): CPU is fine
    s = scores.float().cpu()
    q = torch.exp(alpha * (s - s.max()))
    L = q[:, None] * (x @ x.T) * q[None, :] + 1e-6 * torch.eye(n)
    c = torch.zeros(k, n)
    d2 = torch.diagonal(L).clone()
    picks: list[int] = []
    for t in range(k):
        j = int(torch.argmax(d2))
        if d2[j] <= 1e-10:
            break
        picks.append(j)
        e = (L[j] - c[:t].T @ c[:t, j]) / d2[j].sqrt()
        c[t] = e
        d2 = d2 - e ** 2
        d2[picks] = -float("inf")
    return picks


def metrics_from_ranks(ranks: np.ndarray, ks=(1, 10, 50)) -> dict:
    m = compute_metrics(ranks, list(ks))
    r = m["num_rounds"]
    return {"BRI": round(m["bri"], 4), **{f"R@{k}": m["recall"][k][r] for k in ks},
            **{f"Hits@{k}": m["hits"][k][r] for k in ks}, "median_rank_last": m["median_rank"][r]}


def save(out: str | Path, name: str, obj) -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    p = out / name
    p.write_text(json.dumps(obj, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
    return p


def print_table(rows: list[dict], cols: list[str], first: str = "variant") -> str:
    widths = {c: max(len(c), *(len(_fmt(r.get(c))) for r in rows)) for c in [first] + cols}
    head = "  ".join(f"{c:<{widths[c]}}" if c == first else f"{c:>{widths[c]}}" for c in [first] + cols)
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append("  ".join(f"{_fmt(r.get(c)):<{widths[c]}}" if c == first else f"{_fmt(r.get(c)):>{widths[c]}}"
                               for c in [first] + cols))
    text = "\n".join(lines)
    print(text)
    return text


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.3f}"
    return "" if v is None else str(v)
