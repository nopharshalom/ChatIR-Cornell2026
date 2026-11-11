"""E1 (no LLM): are we showing the questioner the right images?

Replays the queries of a finished run (PlugIR's released reconstructed dialogs by default)
and, before every question, rebuilds the evidence each method would show the questioner:

  plugir_entropy  PlugIR: 10 k-means clusters over the shrinking pool (500 -> 50),
                  lowest caption-entropy image per cluster (search rank ignored)
  plugir_rank     same clusters, best-ranked image per cluster          (proposed fix)
  top10           the 10 best-ranked images
  a2r_dpp10       A2R's DCP: greedy quality x diversity DPP over the top 50
  random10        10 random images from PlugIR's pool                   (control)

For each it measures, against the target image (which the agent never sees):
  target_shown      target is one of the 10 images
  target_nn_sim     cosine similarity of the closest shown image to the target (higher =
                    the questioner sees something that looks like what the user wants)
  query_sim         mean query-image similarity of the shown images (on-topic?)
  diversity         1 - mean pairwise image similarity (do they differ from each other?)
plus pool coverage (is the target inside PlugIR's pool / A2R's top 50 at all?) and, for
PlugIR's saved captions, how well each caption picks out its own image within the pool.

    python experiments/e1_candidate_evidence.py -c configs/base.yaml -c configs/backends/colab.yaml \
        --limit 500 --out runs/e1
"""
from __future__ import annotations

import argparse
import json
import random
import time

import numpy as np
import torch

from common import PLUGIR_RECONED, add_common_args, dpp_greedy, load_retriever_from_args, plugir_pool_size, \
    print_table, save

from agentir.agent.context import select_representatives

METHODS = ["plugir_entropy", "plugir_rank", "top10", "a2r_dpp10", "random10"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--dialogs", default=PLUGIR_RECONED, help="reconstructed dialogs [{img, dialog:[q0..qR]}]")
    ap.add_argument("--clusters", type=int, default=10)
    ap.add_argument("--thres-low", type=int, default=500)
    ap.add_argument("--a2r-top", type=int, default=50)
    a = ap.parse_args()

    retriever, fake_q, _ = load_retriever_from_args(a)
    idx = retriever.index
    if idx.caption_emb is None:
        raise SystemExit("index has no caption embeddings (rebuild with `agentir build-index`)")
    img_all, cap_all = idx.image_emb, idx.caption_emb
    dialogs = ([{"img": q["img"], "dialog": q["dialog"] * (a.rounds + 1)} for q in fake_q] if a.fake
               else json.load(open(a.dialogs)))[: a.limit]
    rng = random.Random(0)

    acc = {m: {k: [] for k in ("target_shown", "target_nn_sim", "query_sim", "diversity", "round")} for m in METHODS}
    cover = {"round": [], "rank": [], "in_plugir_pool": [], "in_top50": []}
    cap_self_rank = []
    t0 = time.time()
    for n, d in enumerate(dialogs):
        t = idx.index_of(d["img"])
        queries = d["dialog"][: a.rounds]  # query before question r is q_{r-1}
        sims_all = retriever.similarity(queries)
        for r0, sims in enumerate(sims_all):
            rnd = r0 + 1
            order = torch.argsort(sims, descending=True)
            rank = int((sims > sims[t]).sum())
            pool = order[: min(plugir_pool_size(rnd, a.thres_low), len(order))]
            top_a2r = order[: a.a2r_top]
            cover["round"].append(rnd)
            cover["rank"].append(rank)
            cover["in_plugir_pool"].append(rank < len(pool))
            cover["in_top50"].append(rank < a.a2r_top)

            seed = rnd  # same clustering for both PlugIR variants -> only the pick differs
            ent = select_representatives(img_all[pool], cap_all[pool], a.clusters, seed, "entropy")
            rk = select_representatives(img_all[pool], cap_all[pool], a.clusters, seed, "rank")
            sel = {
                "plugir_entropy": pool[ent],
                "plugir_rank": pool[rk],
                "top10": order[: a.clusters],
                "a2r_dpp10": top_a2r[dpp_greedy(img_all[top_a2r], sims[top_a2r], a.clusters)],
                "random10": pool[torch.tensor(rng.sample(range(len(pool)), min(a.clusters, len(pool))))],
            }
            tgt = img_all[t]
            for m, s in sel.items():
                e = img_all[s]
                g = e @ e.T
                k = len(s)
                acc[m]["target_shown"].append(bool((s == t).any()))
                acc[m]["target_nn_sim"].append(float((e @ tgt).max()))
                acc[m]["query_sim"].append(float(sims[s].mean()))
                acc[m]["diversity"].append(float(1 - (g.sum() - k) / max(1, k * (k - 1))))
                acc[m]["round"].append(rnd)

            # saved-caption fidelity: rank of each representative's own image, among the pool,
            # when searching with its caption (0 = the caption picks out its image)
            ps = img_all[pool]
            for i in ent:
                s_cap = ps @ cap_all[pool[i]]
                cap_self_rank.append(int((s_cap > s_cap[i]).sum()))
        if n % 100 == 0:
            print(f"{n}/{len(dialogs)} dialogs, {time.time() - t0:.0f}s", flush=True)

    rows, by_round = [], {}
    for m in METHODS:
        A = {k: np.array(v) for k, v in acc[m].items()}
        rows.append({"variant": m, "target_shown_%": 100 * A["target_shown"].mean(),
                     "target_nn_sim": A["target_nn_sim"].mean(), "query_sim": A["query_sim"].mean(),
                     "diversity": A["diversity"].mean()})
        by_round[m] = {k: [float(A[k][A["round"] == r].mean()) for r in range(1, a.rounds + 1)]
                       for k in ("target_shown", "target_nn_sim")}
    C = {k: np.array(v) for k, v in cover.items()}
    coverage = {
        "plugir_pool_size": [plugir_pool_size(r, a.thres_low) for r in range(1, a.rounds + 1)],
        "target_in_plugir_pool_%": [float(100 * C["in_plugir_pool"][C["round"] == r].mean()) for r in range(1, a.rounds + 1)],
        "target_in_top50_%": [float(100 * C["in_top50"][C["round"] == r].mean()) for r in range(1, a.rounds + 1)],
    }
    csr = np.array(cap_self_rank)
    captions = {"median_self_rank_in_pool": float(np.median(csr)),
                "share_caption_not_top10_for_own_image": float((csr >= 10).mean()),
                "share_caption_not_top1_for_own_image": float((csr >= 1).mean())}

    print("\nEvidence shown to the questioner (mean over dialogs x rounds)")
    table = print_table(rows, ["target_shown_%", "target_nn_sim", "query_sim", "diversity"])
    print("\nTarget inside the candidate pool, by round")
    for k, v in coverage.items():
        print(f"  {k:<26}" + " ".join(f"{x:6.1f}" if isinstance(x, float) else f"{x:6d}" for x in v))
    print("\nPlugIR saved captions:", json.dumps(captions))
    save(a.out, "e1_metrics.json", {"dialogs": len(dialogs), "rounds": a.rounds, "summary": rows,
                                    "by_round": by_round, "coverage": coverage, "captions": captions,
                                    "table": table})
    print(f"\nsaved {a.out}/e1_metrics.json")


if __name__ == "__main__":
    main()
