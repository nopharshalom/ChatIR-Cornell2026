"""E3 (VLM): does a question that splits the 10 shown images also split the wider pool?

A2R generates questions from a 10-image grid (DPP over the top 50) and scores them by
expected information gain (IG) on those same 10 images. This tests whether that score
transfers to the other likely candidates.

Per dialog (before question 1 by default):
  S   = A2R's 10 images (greedy DPP over the top 50)                 - shown + scored
  H1  = 15 other images sampled from the top 100                     - alternative scoring set
  H2  = 15 more images from the top 100, disjoint from S and H1      - evaluation only
  1. the VLM sees S as a 2x5 grid and writes 5 questions about visible differences
  2. the VLM answers all 5 questions for every image in S, H1, H2 (one call per image)
  3. the simulated user answers the 5 questions about the target
  4. IG(q, X) = entropy of the answer distribution over X, weighted by retrieval scores
     (deterministic answers, so IG equals the A2R expected uncertainty reduction)
Reported:
  * agreement between IG on S and IG on H1 (Spearman, and same top question?)
  * for each selection rule (by S = A2R, by H1, by S+H1, random): how far the chosen
    question moves the target (change in log rank after appending "q a*" to the query)
    and how much of H2's score mass it rules out given the target's answer.
If choosing by S is clearly worse than choosing by S+H1 on these, idea 03 has support.

~46 VLM calls per dialog. 20 dialogs take roughly 30-60 min on a T4 with qwen3.5:9b.

    python experiments/e3_question_transfer.py -c configs/base.yaml -c configs/backends/colab.yaml \
        --limit 20 --out runs/e3
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import torch

from common import PLUGIR_RECONED, add_common_args, dpp_greedy, load_retriever_from_args, print_table, save

GEN_SYS = ("You help a user find one specific photo in a large collection. You are shown a grid of 10 "
           "candidate photos that all roughly match the user's description. Write questions for the user "
           "whose answers would tell these candidates apart: ask about visible differences between them "
           "(objects, colours, counts, setting, actions, viewpoint), not about things they all share or "
           "things already stated. Each question must be short and answerable by looking at a single photo. "
           "Reply with only a JSON array of {k} strings.")
ANS_SYS = ("Look at the photo and answer each question about it in at most three words. "
           "Use yes or no when possible. If the photo does not show it, answer \"can't tell\". "
           "Reply with only a JSON array of {k} strings, one answer per question, in order.")


def norm_answer(a: str) -> str:
    a = re.sub(r"[^a-z0-9' ]", " ", str(a).lower()).strip()
    a = re.sub(r"\s+", " ", a)
    if re.match(r"^(yes|yeah|yep|correct|true)\b", a):
        return "yes"
    if re.match(r"^(no|nope|not|none|false)\b", a):
        return "no"
    if re.search(r"can ?t tell|cannot tell|unsure|not sure|unknown|unclear|not visible|n a", a):
        return "unknown"
    words = [w for w in a.split() if w not in {"a", "an", "the", "it", "is", "there", "are"}]
    return " ".join(words[:3]) or "unknown"


def parse_list(text: str, k: int) -> list[str]:
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            vals = json.loads(m.group(0))
            return [str(v).strip() for v in vals][:k]
        except json.JSONDecodeError:
            pass
    lines = [re.sub(r"^\s*(?:[-*\d.)\]]+\s*)", "", x).strip(" \"',") for x in text.splitlines()]
    return [x for x in lines if x][:k]


def answer_ig(answers: list[str], weights: np.ndarray) -> float:
    """Entropy (nats) of the weighted answer distribution = expected information gain."""
    mass: dict[str, float] = {}
    for a, w in zip(answers, weights):
        mass[a] = mass.get(a, 0.0) + float(w)
    p = np.array(list(mass.values()))
    p = p / p.sum()
    return float(-(p * np.log(p + 1e-12)).sum())


def spearman(x, y) -> float:
    x, y = np.asarray(x, float), np.asarray(y, float)
    if np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    rx, ry = x.argsort().argsort(), y.argsort().argsort()
    return float(np.corrcoef(rx, ry)[0, 1])


def make_grid(paths: list[str], cell: int = 256) -> str:
    from PIL import Image, ImageDraw

    grid = Image.new("RGB", (5 * cell, 2 * cell), "white")
    for i, p in enumerate(paths[:10]):
        im = Image.open(p).convert("RGB")
        im.thumbnail((cell - 6, cell - 6))
        x, y = (i % 5) * cell + 3, (i // 5) * cell + 3
        grid.paste(im, (x, y))
        ImageDraw.Draw(grid).text((x + 4, y + 2), str(i + 1), fill="yellow")
    f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    grid.save(f.name, quality=90)
    return f.name


class VLMOracle:
    """Real models: question generator from the grid, per-image answerer, simulated user."""

    def __init__(self, cfg, corpus_dir: str, k: int):
        from agentir.runtime import build_user_sim, make_llm

        self.llm = make_llm(cfg.agent.llm)
        self.user = build_user_sim(cfg, agent_llm=self.llm)
        self.dir, self.k = corpus_dir, k

    def _path(self, rel):
        return str(Path(self.dir) / rel)

    def questions(self, query: str, shown: list[str]) -> list[str]:
        from agentir.llm import user_message_with_image

        grid = make_grid([self._path(p) for p in shown])
        msgs = [{"role": "system", "content": GEN_SYS.format(k=self.k)},
                user_message_with_image(f"User's description: {query}\nWrite {self.k} questions.", grid, 1280)]
        qs = parse_list(self.llm.complete(msgs, temperature=0.0, max_tokens=256, tag="e3_generate"), self.k)
        return [q if q.endswith("?") else q + "?" for q in dict.fromkeys(qs)]

    def answers(self, rel: str, questions: list[str]) -> list[str]:
        from agentir.llm import user_message_with_image

        text = "".join(f"{i + 1}. {q}\n" for i, q in enumerate(questions))
        msgs = [{"role": "system", "content": ANS_SYS.format(k=len(questions))},
                user_message_with_image(text, self._path(rel), 512)]
        out = parse_list(self.llm.complete(msgs, temperature=0.0, max_tokens=96, tag="e3_answer"), len(questions))
        return [norm_answer(x) for x in out + ["unknown"] * (len(questions) - len(out))]

    def user_answer(self, target: str, q: str) -> str:
        return self.user.answer(self._path(target), q, None)


class FakeOracle:
    """Synthetic world: template questions, answers read from the hidden attributes."""

    def __init__(self, lookup, k: int):
        from agentir.smoke import QUESTIONS, scripted_answer

        self.lookup, self.k, self.Q = lookup, k, QUESTIONS
        self.ans = scripted_answer(lookup)

    def questions(self, query, shown):
        obj = next((a["obj"] for p, a in self.lookup.items() if a["obj"] in query), "object")
        return [t.format(obj=obj) for t in self.Q.values()][: self.k]

    def answers(self, rel, questions):
        return [norm_answer(self.ans(rel, q)) for q in questions]

    def user_answer(self, target, q):
        return self.ans(target, q)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--dialogs", default=PLUGIR_RECONED,
                    help="queries to start from; --round picks which one (0 = the user's description)")
    ap.add_argument("--round", type=int, default=0)
    ap.add_argument("--start", type=int, default=0, help="skip the first N dialogs")
    ap.add_argument("--num-questions", type=int, default=5)
    ap.add_argument("--wide", type=int, default=100, help="H1/H2 are drawn from this many top results")
    ap.add_argument("--h-size", type=int, default=15)
    ap.add_argument("--temperature", type=float, default=0.05, help="score softmax temperature for weights")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    retriever, fake_q, lookup = load_retriever_from_args(a)
    idx = retriever.index
    if a.fake:
        dialogs, oracle = fake_q, FakeOracle(lookup, a.num_questions)
    else:
        from agentir.config import load_config

        cfg = load_config(a.config, a.set)
        dialogs, oracle = json.load(open(a.dialogs)), VLMOracle(cfg, cfg.data.corpus_dir, a.num_questions)
    dialogs = dialogs[a.start :][: a.limit]
    rng = random.Random(1021)

    per = []
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "e3_dialogs.jsonl", "w")
    for n, d in enumerate(dialogs):
        query = d["dialog"][min(a.round, len(d["dialog"]) - 1)]
        t = idx.index_of(d["img"])
        sims = retriever.similarity([query])[0]
        order = torch.argsort(sims, descending=True)
        top50 = order[:50]
        S = [int(top50[i]) for i in dpp_greedy(idx.image_emb[top50], sims[top50], 10)]
        rest = [int(i) for i in order[: a.wide] if int(i) not in S]
        rng.shuffle(rest)
        H1, H2 = rest[: a.h_size], rest[a.h_size : 2 * a.h_size]
        paths = lambda ids: [idx.paths[i] for i in ids]  # noqa: E731

        qs = oracle.questions(query, paths(S))
        if len(qs) < 2:
            print(f"dialog {n}: fewer than 2 questions, skipped")
            continue
        imgs = S + H1 + H2
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            ans = list(ex.map(lambda i: oracle.answers(idx.paths[i], qs), imgs))
            user = list(ex.map(lambda q: oracle.user_answer(d["img"], q), qs))
        A = {i: x for i, x in zip(imgs, ans)}
        w = lambda ids: torch.softmax(sims[ids] / a.temperature, 0).cpu().numpy()  # noqa: E731
        ig = {name: [answer_ig([A[i][j] for i in ids], w(ids)) for j in range(len(qs))]
              for name, ids in (("S", S), ("H1", H1), ("SH1", S + H1))}

        base_rank = int((sims > sims[t]).sum())
        new_ranks = retriever.ranks_of([f"{query}, {q} {u}" for q, u in zip(qs, user)], t)
        dlog = [math.log1p(r) - math.log1p(base_rank) for r in new_ranks]  # < 0 = target moved up
        u_norm = [norm_answer(u) for u in user]
        wH2 = w(H2)
        elim = [float(sum(wt for i, wt in zip(H2, wH2) if A[i][j] != u_norm[j])) if u_norm[j] != "unknown" else 0.0
                for j in range(len(qs))]
        rec = {"n": n, "img": d["img"], "query": query, "questions": qs, "user_answers": user,
               "ig": ig, "base_rank": base_rank, "new_ranks": new_ranks, "dlogrank": dlog,
               "h2_mass_ruled_out": elim, "answers_S": [A[i] for i in S]}
        per.append(rec)
        log.write(json.dumps(rec) + "\n")
        log.flush()
        print(f"{n + 1}/{len(dialogs)}  base rank {base_rank}  IG_S {np.round(ig['S'], 2).tolist()}  "
              f"IG_H1 {np.round(ig['H1'], 2).tolist()}", flush=True)

    if not per:
        raise SystemExit("no dialogs scored")
    rho = [spearman(p["ig"]["S"], p["ig"]["H1"]) for p in per]
    same_top = [int(np.argmax(p["ig"]["S"]) == np.argmax(p["ig"]["H1"])) for p in per]
    chance = float(np.mean([1 / len(p["questions"]) for p in per]))
    rules = {"by_S (A2R)": "S", "by_H1": "H1", "by_S+H1": "SH1"}
    rows = []
    for name, key in rules.items():
        pick = [int(np.argmax(p["ig"][key])) for p in per]
        rows.append({"variant": name,
                     "dlogrank": float(np.mean([p["dlogrank"][j] for p, j in zip(per, pick)])),
                     "target_up_%": 100 * float(np.mean([p["dlogrank"][j] < 0 for p, j in zip(per, pick)])),
                     "H2_ruled_out": float(np.mean([p["h2_mass_ruled_out"][j] for p, j in zip(per, pick)]))})
    rows.append({"variant": "random", "dlogrank": float(np.mean([np.mean(p["dlogrank"]) for p in per])),
                 "target_up_%": 100 * float(np.mean([np.mean(np.array(p["dlogrank"]) < 0) for p in per])),
                 "H2_ruled_out": float(np.mean([np.mean(p["h2_mass_ruled_out"]) for p in per]))})
    rows.append({"variant": "oracle (best for target)", "dlogrank": float(np.mean([min(p["dlogrank"]) for p in per])),
                 "target_up_%": 100 * float(np.mean([min(p["dlogrank"]) < 0 for p in per])),
                 "H2_ruled_out": float(np.mean([max(p["h2_mass_ruled_out"]) for p in per]))})
    agree = {"dialogs": len(per), "spearman_IG_S_vs_IG_H1_mean": float(np.nanmean(rho)),
             "same_top_question_%": 100 * float(np.mean(same_top)), "chance_same_top_%": 100 * chance,
             "spearman_IG_S_vs_target_gain": float(np.nanmean([spearman(p["ig"]["S"], [-x for x in p["dlogrank"]]) for p in per])),
             "spearman_IG_SH1_vs_target_gain": float(np.nanmean([spearman(p["ig"]["SH1"], [-x for x in p["dlogrank"]]) for p in per]))}
    print("\nDoes IG on the 10 shown images agree with IG on other top-100 images?")
    for k, v in agree.items():
        print(f"  {k:<34} {round(v, 3) if isinstance(v, float) else v}")
    print("\nQuestion chosen by each rule (dlogrank < 0 = target moved up; H2 = held-out images)")
    table = print_table(rows, ["dlogrank", "target_up_%", "H2_ruled_out"])
    save(a.out, "e3_metrics.json", {"agreement": agree, "selection": rows, "table": table, "round": a.round})
    print(f"\nsaved {a.out}/e3_metrics.json")


if __name__ == "__main__":
    main()
