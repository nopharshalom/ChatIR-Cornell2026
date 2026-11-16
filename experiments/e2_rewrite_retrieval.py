"""E2b (no LLM): do rewrite errors cost retrieval, and do cheap fixes recover it?

Scores the rewritten queries of a finished run (PlugIR's released dialogs by default)
with the BLIP index, then re-scores simple text-only interventions:

  rewrite           the queries as produced (baseline)
  caption_only      the user's original description every round (do rewrites help at all?)
  anchored          original description + ". " + rewrite (puts back anything dropped)
  leak_stripped     rewrite minus sentences/clauses that cite "retrieval candidates" etc.
  anchored_stripped both

It also reports how often a rewrite makes the target's rank worse, and whether rounds
where the rewrite leaked prompt text, or dropped a detail (from e2_rewrite_drift_text.py),
hurt more than other rounds.

    python experiments/e2_rewrite_retrieval.py -c configs/base.yaml -c configs/backends/colab.yaml \
        --drift runs/e2a/e2a_metrics.json --out runs/e2b
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from common import PLUGIR_RECONED, add_common_args, load_retriever_from_args, metrics_from_ranks, print_table, save

from agentir.agent.reformulators import LEAK_RE, strip_leaked_sentences


def variants(q: list[str]) -> dict[str, list[str]]:
    c = q[0]
    anch = lambda x: x if x.lower().startswith(c.lower()) else f"{c}. {x}"  # noqa: E731
    strip = lambda x: strip_leaked_sentences(x) or c  # noqa: E731
    return {
        "rewrite": q,
        "caption_only": [c] * len(q),
        "anchored": [c] + [anch(x) for x in q[1:]],
        "leak_stripped": [c] + [strip(x) for x in q[1:]],
        "anchored_stripped": [c] + [anch(strip(x)) for x in q[1:]],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--dialogs", default=PLUGIR_RECONED)
    ap.add_argument("--drift", help="e2a_metrics.json from e2_rewrite_drift_text.py on the same dialogs")
    a = ap.parse_args()

    retriever, fake_q, _ = load_retriever_from_args(a)
    if a.fake:
        dialogs = [{"img": q["img"], "dialog": [q["dialog"][0] + (f", clue {r}" if r else "") +
                                                 (" among the retrieval candidates" if r == 2 else "")
                                                 for r in range(a.rounds + 1)]} for q in fake_q]
    else:
        dialogs = json.load(open(a.dialogs))
    dialogs = dialogs[: a.limit]
    R = a.rounds

    ranks = {}
    for d in dialogs:
        t = retriever.index.index_of(d["img"])
        for name, qs in variants(d["dialog"][: R + 1]).items():
            ranks.setdefault(name, []).append(retriever.ranks_of(qs, t))
    ranks = {k: np.array(v) for k, v in ranks.items()}

    rows = [{"variant": k, **metrics_from_ranks(v)} for k, v in ranks.items()]
    print(f"\n{len(dialogs)} dialogs x {R} rounds  (BRI lower is better)")
    table = print_table(rows, ["BRI", "R@1", "R@10", "R@50", "Hits@10", "median_rank_last"])

    base = ranks["rewrite"]
    dlog = np.log1p(base[:, 1:]) - np.log1p(base[:, :-1])  # >0 = the new rewrite ranks the target worse
    leak = np.array([[bool(LEAK_RE.search(x)) for x in d["dialog"][1 : R + 1]] for d in dialogs])
    stats = {
        "share_rounds_rank_worse": float((base[:, 1:] > base[:, :-1]).mean()),
        "share_rounds_rank_2x_worse": float((base[:, 1:] + 1 > 2 * (base[:, :-1] + 1)).mean()),
        "share_dialogs_final_worse_than_caption": float((base[:, -1] > base[:, 0]).mean()),
        "share_dialogs_final_worse_than_best_round": float((base[:, -1] > base.min(1)).mean()),
        "leak_rounds": int(leak.sum()),
        "mean_dlogrank_leak_rounds": float(dlog[leak].mean()) if leak.any() else None,
        "mean_dlogrank_other_rounds": float(dlog[~leak].mean()),
    }
    if a.drift:
        drops = np.array(json.load(open(a.drift))["_per_dialog"]["drops"])[: len(dialogs), :R] > 0
        stats.update({"drop_rounds": int(drops.sum()),
                      "mean_dlogrank_rounds_dropping_a_detail": float(dlog[drops].mean()),
                      "mean_dlogrank_rounds_keeping_details": float(dlog[~drops].mean())})
    print("\nRewrite regressions (dlogrank > 0 means the rewrite pushed the target down)")
    for k, v in stats.items():
        print(f"  {k:<42} {v if not isinstance(v, float) else round(v, 4)}")
    save(a.out, "e2b_metrics.json", {"dialogs": len(dialogs), "rounds": R, "summary": rows, "regressions": stats,
                                     "table": table})
    np.savez_compressed(f"{a.out}/e2b_ranks.npz", **ranks)
    print(f"\nsaved {a.out}/e2b_metrics.json")


if __name__ == "__main__":
    main()
