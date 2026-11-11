"""E2c (LLM, rewrite step only): replay a run's questions and answers through other rewriters.

The questions and answers of a finished `agentir run` stay fixed; only the dialog ->
query rewrite changes, so any difference in Recall/BRI is caused by the rewrite alone.
One LLM call per round per variant (the `plugir` variant is usually free: it hits the
LLM cache of the original run).

  plugir                 PlugIR's prompt (what the run used)
  plugir_anchor_strip    + original description prepended, leaked clauses removed
  grounded               rewrite told to keep original details, add only stated facts,
                         never mention candidates
  grounded_anchor        + original description prepended
  concat                 no LLM: description + "q a" turns joined (ChatIR)

    python experiments/e2_replay_reformulators.py -c configs/base.yaml -c configs/backends/colab.yaml \
        --run runs/plugir-<fingerprint> --out runs/e2c
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from common import add_common_args, load_retriever_from_args, metrics_from_ranks, print_table, save

from agentir.agent.reformulators import LEAK_RE, build_reformulator
from agentir.agent.state import DialogState, Turn
from agentir.config import ReformulatorConfig

VARIANTS = {
    "plugir": {"kind": "plugir"},
    "plugir_anchor_strip": {"kind": "plugir", "anchor_caption": True, "strip_leaks": True},
    "grounded": {"kind": "grounded"},
    "grounded_anchor": {"kind": "grounded", "anchor_caption": True},
    "concat": {"kind": "concat"},
}


def replay(rec: dict, reformulator, llm, rounds: int) -> list[str]:
    st = DialogState(caption=rec["caption"])
    for r in rec["rounds"][:rounds]:
        st.turns.append(Turn(r["question"].strip(), r["answer"].strip()))
        st.query = reformulator.reformulate(st, llm)
        st.query_history.append(st.query)
    return st.query_history


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--run", required=True, help="run directory containing sessions.jsonl")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    retriever, _, _ = load_retriever_from_args(a)
    if a.fake:
        from agentir.smoke import MockLLM

        llm = MockLLM()
    else:
        from agentir.config import load_config
        from agentir.runtime import make_llm

        llm = make_llm(load_config(a.config, a.set).agent.llm)
    recs = [json.loads(x) for x in open(Path(a.run) / "sessions.jsonl") if x.strip()][: a.limit]
    R = min(a.rounds, min(len(r["rounds"]) for r in recs))

    rows, queries, ranks = [], {}, {}
    for name in a.variants.split(","):
        ref = build_reformulator(ReformulatorConfig(**VARIANTS[name]))
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            qs = list(ex.map(lambda rec: replay(rec, ref, llm, R), recs))
        rk = np.array([retriever.ranks_of(q, retriever.index.index_of(rec["target"])) for q, rec in zip(qs, recs)])
        leak = np.mean([[bool(LEAK_RE.search(x)) for x in q[1:]] for q in qs])
        words = np.mean([len(x.split()) for q in qs for x in q[1:]])
        rows.append({"variant": name, **metrics_from_ranks(rk), "leak_%": 100 * leak, "words": words})
        queries[name], ranks[name] = qs, rk.tolist()
        print(f"done {name}", flush=True)

    # the original run's own queries, as a check that the `plugir` replay reproduces it
    orig = np.array([r["ranks"][: R + 1] for r in recs])
    rows.insert(0, {"variant": "run_as_recorded", **metrics_from_ranks(orig)})
    print(f"\n{len(recs)} sessions x {R} rounds, same questions and answers for every variant")
    table = print_table(rows, ["BRI", "R@1", "R@10", "Hits@10", "leak_%", "words"])
    save(a.out, "e2c_metrics.json", {"run": a.run, "sessions": len(recs), "rounds": R, "summary": rows,
                                     "table": table})
    save(a.out, "e2c_queries.json", {"queries": queries, "ranks": ranks,
                                     "qids": [r["qid"] for r in recs]})
    print(f"\nsaved {a.out}/e2c_metrics.json")


if __name__ == "__main__":
    main()
