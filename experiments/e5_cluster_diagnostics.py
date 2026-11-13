"""E5 (no LLM): where is the target relative to PlugIR's k-means clusters, round by round?

Reads a finished run (runs/<name>-<fp>/sessions.jsonl + config.yaml), rebuilds each round's
candidate pool and clusters from the logged queries, and records against the target:

  in_pool          target is inside the pool that gets clustered
  is_rep           target is one of the shown representatives (from the run's own log)
  rank             target's rank under the query the questioner saw
  sim_nearest_rep  cosine sim of the target to the closest shown representative
  sim_own_rep      ... to the representative of the target's own cluster
  sim_centroid     ... to its own cluster's centroid
  own_cluster_size size of the target's cluster
  enters_top10     target is in the top 10 after this round's answer, but was not before

Summary (summary.json):
  ever_rep_rate        fraction of dialogs whose target is a representative in >= 1 round
  ever_in_pool_rate    same for "inside the pool"
  hits10_given_ever_rep / hits10_given_never_rep
  convergence: is closeness of the target to the shown captions associated with the target
               entering the top 10 on the next query? (logistic-style comparison of means,
               plus the "round just before convergence" vs. all other rounds)
  rep_match_rate       how often the rebuilt clusters reproduce the logged representatives
                       (sanity check that the rebuild matches the run)

Figures: per-round 2-D projections (PCA of the pool's image embeddings) with clusters,
representatives and the target, for --plot-qids (default: 3 dialogs that converge, 3 that don't).

    python experiments/e5_cluster_diagnostics.py -c configs/base.yaml -c configs/backends/colab.yaml \
        --run runs/e1b_plugir-f4b10a69e035 --out runs/e5_plugir
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

from common import add_common_args, load_retriever_from_args, save

from agentir.agent.context import candidate_pool, select_representatives, spherical_kmeans
from agentir.agent.state import DialogState


def load_run(run: Path):
    import yaml

    recs = [json.loads(x) for x in open(run / "sessions.jsonl") if x.strip()]
    cfg = yaml.safe_load(open(run / "config.yaml")) if (run / "config.yaml").exists() else {}
    return recs, cfg


def rebuild(rec, idx, retriever, gen: dict, seed: int, rounds: int):
    """Per-round rows for one dialog."""
    t = idx.index_of(rec["target"])
    timg = torch.nn.functional.normalize(idx.image_emb[t].float(), dim=-1)
    state = DialogState(caption=rec["caption"])
    rows, ranks = [], rec["ranks"]
    sims_all = retriever.similarity(rec["queries"][:rounds])  # [R, N]
    for r in range(1, min(rounds, len(rec["rounds"])) + 1):
        state.turns = [None] * (r - 1)  # only len() matters for the round number
        sims = sims_all[r - 1]
        pool = candidate_pool(state, sims, gen.get("thres_low", 500), gen.get("pool_schedule", "shrink"),
                              gen.get("cum_per_round", 200), gen.get("cum_keep", 100))
        img = idx.image_emb[pool]
        k = gen.get("n_clusters", 10)
        rseed = seed + r
        labels = spherical_kmeans(img, k, seed=rseed)
        picks = select_representatives(img, idx.caption_emb[pool], k, rseed, gen.get("representative", "entropy"))
        rebuilt = [int(pool[i]) for i in picks]
        logged = (rec["rounds"][r - 1].get("debug") or {}).get("context_indices") or rebuilt
        reps = torch.nn.functional.normalize(idx.image_emb[logged].float(), dim=-1)
        pos = (pool == t).nonzero()
        in_pool = bool(len(pos))
        row = {"qid": rec["qid"], "round": r, "in_pool": in_pool, "is_rep": t in logged,
               "rank": int(ranks[r - 1]), "rank_after": int(ranks[r]),
               "sim_nearest_rep": float((reps @ timg).max()), "pool_size": int(pool.numel()),
               "rep_match": len(set(rebuilt) & set(logged)) / max(1, len(logged))}
        if in_pool:
            p = int(pos[0])
            c = int(labels[p])
            members = (labels == c).nonzero().flatten()
            cen = torch.nn.functional.normalize(
                torch.nn.functional.normalize(img[members].float(), dim=-1).mean(0), dim=-1)
            own_rep = [i for i in rebuilt if int(labels[(pool == i).nonzero()[0]]) == c]
            row.update(own_cluster_size=int(len(members)), sim_centroid=float(cen @ timg),
                       sim_own_rep=float(torch.nn.functional.normalize(
                           idx.image_emb[own_rep[0]].float(), dim=-1) @ timg) if own_rep else None)
        row["enters_top10"] = row["rank_after"] < 10 and min(ranks[:r]) >= 10
        rows.append(row)
    return rows


def summarize(rows: list[dict], recs: list[dict]) -> dict:
    by_q: dict[int, list[dict]] = {}
    for r in rows:
        by_q.setdefault(r["qid"], []).append(r)
    final_hit = {rec["qid"]: min(rec["ranks"]) < 10 for rec in recs}
    ever_rep = {q: any(x["is_rep"] for x in v) for q, v in by_q.items()}
    ever_pool = {q: any(x["in_pool"] for x in v) for q, v in by_q.items()}

    def rate(d):
        return round(100 * float(np.mean(list(d.values()))), 2) if d else None

    def cond(flag):
        qs = [q for q in by_q if ever_rep[q] == flag]
        return round(100 * float(np.mean([final_hit[q] for q in qs])), 2) if qs else None

    # rounds where the target was not yet found: does closeness predict finding it next?
    open_rows = [r for r in rows if r["rank"] >= 10 and min(r2["rank"] for r2 in by_q[r["qid"]]
                                                           if r2["round"] <= r["round"]) >= 10]
    hit = [r for r in open_rows if r["enters_top10"]]
    miss = [r for r in open_rows if not r["enters_top10"]]

    def mean(rs, k):
        v = [r[k] for r in rs if r.get(k) is not None]
        return round(float(np.mean(v)), 4) if v else None

    conv = {k: {"before_convergence": mean(hit, k), "other_open_rounds": mean(miss, k)}
            for k in ("sim_nearest_rep", "sim_own_rep", "sim_centroid", "rank", "own_cluster_size")}
    conv["is_rep_rate"] = {"before_convergence": mean([{**r, "x": float(r["is_rep"])} for r in hit], "x"),
                           "other_open_rounds": mean([{**r, "x": float(r["is_rep"])} for r in miss], "x")}
    conv["n_rounds"] = {"before_convergence": len(hit), "other_open_rounds": len(miss)}
    # AUC of sim_nearest_rep for predicting "enters top 10 next round" (rank-based, no sklearn)
    a = np.array([r["sim_nearest_rep"] for r in hit])
    b = np.array([r["sim_nearest_rep"] for r in miss])
    auc = float(((a[:, None] > b[None, :]).mean() + 0.5 * (a[:, None] == b[None, :]).mean())) if len(a) and len(b) else None
    per_round = {}
    for rd in sorted({r["round"] for r in rows}):
        rr = [r for r in rows if r["round"] == rd]
        per_round[rd] = {"in_pool": round(100 * np.mean([r["in_pool"] for r in rr]), 2),
                         "is_rep": round(100 * np.mean([r["is_rep"] for r in rr]), 2),
                         "sim_nearest_rep": round(float(np.mean([r["sim_nearest_rep"] for r in rr])), 4)}
    return {"num_dialogs": len(by_q), "ever_rep_rate": rate(ever_rep), "ever_in_pool_rate": rate(ever_pool),
            "final_hits10": rate(final_hit),
            "hits10_given_ever_rep": cond(True), "hits10_given_never_rep": cond(False),
            "dialogs_ever_rep": int(sum(ever_rep.values())),
            "convergence": conv, "auc_sim_nearest_rep_predicts_entry": None if auc is None else round(auc, 3),
            "per_round": per_round, "rep_match_rate": mean(rows, "rep_match")}


def plot_dialog(rec, idx, retriever, gen, seed, rounds, out: Path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = idx.index_of(rec["target"])
    sims_all = retriever.similarity(rec["queries"][:rounds])
    state = DialogState(caption=rec["caption"])
    n = min(rounds, len(rec["rounds"]))
    cols = 5
    fig, axes = plt.subplots((n + cols - 1) // cols, cols, figsize=(3.2 * cols, 3.1 * ((n + cols - 1) // cols)))
    axes = np.atleast_1d(axes).ravel()
    cmap = plt.get_cmap("tab10")
    for r in range(1, n + 1):
        ax = axes[r - 1]
        state.turns = [None] * (r - 1)
        pool = candidate_pool(state, sims_all[r - 1], gen.get("thres_low", 500), gen.get("pool_schedule", "shrink"),
                              gen.get("cum_per_round", 200), gen.get("cum_keep", 100))
        img = torch.nn.functional.normalize(idx.image_emb[pool].float(), dim=-1)
        k = gen.get("n_clusters", 10)
        labels = spherical_kmeans(img, k, seed=seed + r).cpu().numpy()
        logged = (rec["rounds"][r - 1].get("debug") or {}).get("context_indices") or []
        tv = torch.nn.functional.normalize(idx.image_emb[t].float(), dim=-1)[None]
        X = torch.cat([img, tv]).cpu().numpy()
        X = X - X[:-1].mean(0)
        _, _, vt = np.linalg.svd(X[:-1], full_matrices=False)
        P = X @ vt[:2].T
        ax.scatter(P[:-1, 0], P[:-1, 1], c=[cmap(l % 10) for l in labels], s=8, alpha=0.55, linewidths=0)
        pos = {int(i): j for j, i in enumerate(pool.tolist())}
        rp = [pos[i] for i in logged if i in pos]
        ax.scatter(P[rp, 0], P[rp, 1], facecolors="none", edgecolors="black", s=46, linewidths=1.2)
        ax.scatter(P[-1, 0], P[-1, 1], marker="*", c="crimson", s=170, edgecolors="white", linewidths=0.8, zorder=5)
        ax.set_title(f"round {r}: rank {rec['ranks'][r - 1] + 1}" + (" (rep)" if t in logged else ""), fontsize=9)
        ax.set_xticks([]), ax.set_yticks([])
    for ax in axes[n:]:
        ax.axis("off")
    best = min(rec["ranks"]) + 1
    fig.suptitle(f"qid {rec['qid']}: pool clusters (colours), shown representatives (circles), "
                 f"target (star). Best rank {best}", fontsize=10)
    fig.tight_layout()
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"clusters_qid{rec['qid']}.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--run", required=True, help="run directory with sessions.jsonl (+ config.yaml)")
    ap.add_argument("--plot-qids", type=int, nargs="*", default=None)
    ap.add_argument("--n-plots", type=int, default=3, help="per group (converging / not) when --plot-qids is unset")
    a = ap.parse_args()

    retriever, _, _ = load_retriever_from_args(a)
    idx = retriever.index
    recs, cfg = load_run(Path(a.run))
    recs = recs[: a.limit] if a.limit else recs
    gen = ((cfg.get("agent") or {}).get("generator") or {})
    seed = int(cfg.get("seed", 1021))
    rows = []
    for i, rec in enumerate(recs):
        rows.extend(rebuild(rec, idx, retriever, gen, seed, a.rounds))
        if (i + 1) % 25 == 0:
            print(f"{i + 1}/{len(recs)} dialogs", file=sys.stderr)
    summ = summarize(rows, recs)
    out = Path(a.out)
    save(out, "rows.json", rows)
    save(out, "summary.json", summ)
    print(json.dumps({k: v for k, v in summ.items() if k != "per_round"}, indent=2))
    qids = a.plot_qids
    if qids is None:
        conv = [r for r in recs if min(r["ranks"][1:]) < 10 and r["ranks"][0] >= 10]
        never = [r for r in recs if min(r["ranks"]) >= 10]
        qids = [r["qid"] for r in conv[: a.n_plots] + never[: a.n_plots]]
    for rec in recs:
        if rec["qid"] in qids:
            print("figure:", plot_dialog(rec, idx, retriever, gen, seed, a.rounds, out / "figs"))


if __name__ == "__main__":
    main()
