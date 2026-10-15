"""Round-wise metrics, matching PlugIR's eval.py exactly.

Input: `ranks[n, r]` = 0-based rank of target n after round r (r = 0 is the caption).
* Recall@K[r]: fraction of sessions whose *current* query ranks the target in the top K.
* Hits@K[r]:   fraction whose target has been in the top K at *any* round <= r
               (the user would have spotted it and stopped).
* BRI:         Best log Rank Integral (PlugIR): trapezoid-rule mean over rounds of
               log(1 + best rank so far), averaged over sessions. Lower is better.
"""
from __future__ import annotations

import numpy as np


def compute_metrics(ranks: np.ndarray, ks: list[int]) -> dict:
    ranks = np.asarray(ranks, dtype=np.float64)
    if ranks.ndim != 2 or ranks.shape[1] < 2:
        raise ValueError("ranks must be [num_sessions, num_rounds+1] with at least one Q&A round")
    best = np.minimum.accumulate(ranks, axis=1)
    out: dict = {"num_sessions": int(ranks.shape[0]), "num_rounds": int(ranks.shape[1] - 1)}
    out["recall"] = {int(k): ((ranks < k).mean(0) * 100).round(4).tolist() for k in ks}
    out["hits"] = {int(k): ((best < k).mean(0) * 100).round(4).tolist() for k in ks}
    logs = np.log(best + 1.0)
    per_session = ((logs[:, :-1] + logs[:, 1:]) / 2).mean(axis=1)
    out["bri"] = float(per_session.mean())
    out["mean_rank"] = (ranks + 1).mean(0).round(2).tolist()
    out["median_rank"] = np.median(ranks + 1, axis=0).tolist()
    return out


def format_table(m: dict) -> str:
    rounds = range(m["num_rounds"] + 1)
    head = "metric      " + "".join(f"{r:>7}" for r in rounds)
    lines = [head, "-" * len(head)]
    for name in ("recall", "hits"):
        for k, vals in m[name].items():
            label = f"{'Recall' if name == 'recall' else 'Hits'}@{k}"
            lines.append(f"{label:<12}" + "".join(f"{v:7.2f}" for v in vals))
    lines.append(f"{'MedianRank':<12}" + "".join(f"{v:7.0f}" for v in m["median_rank"]))
    lines.append(f"BRI (lower is better): {m['bri']:.4f}   sessions: {m['num_sessions']}")
    return "\n".join(lines)
