"""Ping-pong between the agent and the simulated user, plus scoring.

    orchestrator ──ask──▶ agent        (question)
    orchestrator ──answer──▶ user-sim  (answer; only the user-sim sees the target image)
    orchestrator ──observe──▶ agent    (new retrieval query)
    ... up to max_rounds, then the evaluator ranks the target under every query.

Handles hide whether each side runs in-process or behind its HTTP service, so the
same loop drives a laptop dev run and a two-service GPU run.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from tqdm import tqdm

from .config import RunConfig, save_config
from .data import Sample
from .metrics import compute_metrics, format_table

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- handles
class AgentHandle(Protocol):
    def start(self, sid: str, caption: str) -> str: ...
    def ask(self, sid: str) -> dict: ...
    def observe(self, sid: str, question: str, answer: str) -> dict: ...
    def close(self, sid: str) -> None: ...


class UserHandle(Protocol):
    def answer(self, target: str, question: str, context: str) -> str: ...


class LocalAgentHandle:
    def __init__(self, agent):
        self.agent = agent
        self.states: dict[str, Any] = {}
        self._lock = threading.Lock()

    def start(self, sid, caption):
        st = self.agent.start(caption)
        with self._lock:
            self.states[sid] = st
        return st.query

    def ask(self, sid):
        d = self.agent.ask(self.states[sid])
        return {"question": d.question, "candidates": d.candidates, "scores": d.scores, "debug": d.debug}

    def observe(self, sid, question, answer):
        st = self.states[sid]
        return {"query": self.agent.observe(st, question, answer), "cost": dict(st.cost)}

    def close(self, sid):
        with self._lock:
            self.states.pop(sid, None)


class HttpAgentHandle:
    def __init__(self, url: str, timeout: float = 600.0):
        import httpx

        self.c = httpx.Client(base_url=url, timeout=timeout)

    def _post(self, path, payload=None):
        r = self.c.post(path, json=payload or {})
        r.raise_for_status()
        return r.json()

    def start(self, sid, caption):
        return self._post("/sessions", {"session_id": sid, "caption": caption})["query"]

    def ask(self, sid):
        return self._post(f"/sessions/{sid}/ask")

    def observe(self, sid, question, answer):
        return self._post(f"/sessions/{sid}/observe", {"question": question, "answer": answer})

    def close(self, sid):
        self.c.delete(f"/sessions/{sid}")


class LocalUserHandle:
    def __init__(self, user_sim, corpus_dir: str):
        self.user_sim = user_sim
        self.corpus_dir = corpus_dir

    def answer(self, target, question, context):
        return self.user_sim.answer(str(Path(self.corpus_dir) / target), question, context)


class HttpUserHandle:
    def __init__(self, url: str, timeout: float = 600.0):
        import httpx

        self.c = httpx.Client(base_url=url, timeout=timeout)

    def answer(self, target, question, context):
        r = self.c.post("/answer", json={"target": target, "question": question, "context": context})
        r.raise_for_status()
        return r.json()["answer"]


# ----------------------------------------------------------------------------- one session
def run_session(sample: Sample, agent: AgentHandle, user: UserHandle, retriever, max_rounds: int,
                run_name: str = "run") -> dict:
    sid = f"{run_name}-{sample.qid}"
    t_start = time.time()
    queries = [agent.start(sid, sample.caption)]
    rounds, cost = [], {}
    try:
        for r in range(1, max_rounds + 1):
            t0 = time.time()
            d = agent.ask(sid)
            answer = user.answer(sample.target, d["question"], queries[-1])
            obs = agent.observe(sid, d["question"], answer)
            queries.append(obs["query"])
            cost = obs.get("cost", cost)
            rounds.append({"round": r, "question": d["question"], "answer": answer, "query": obs["query"],
                           "candidates": d.get("candidates"), "scores": d.get("scores"),
                           "debug": d.get("debug"), "seconds": round(time.time() - t0, 3)})
    finally:
        agent.close(sid)
    ranks = retriever.ranks_of(queries, retriever.index.index_of(sample.target))
    return {"qid": sample.qid, "target": sample.target, "caption": sample.caption, "queries": queries,
            "ranks": ranks, "rounds": rounds, "cost": cost, "seconds": round(time.time() - t_start, 2)}


# ----------------------------------------------------------------------------- full run
def _read_done(path: Path) -> dict[int, dict]:
    done: dict[int, dict] = {}
    if path.exists():
        with open(path) as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    done[rec["qid"]] = rec
    return done


def run_dir(cfg: RunConfig) -> Path:
    """runs/<name>-<fingerprint>: changing any result-affecting setting starts a fresh directory,
    so resuming can never mix sessions produced under different settings."""
    return Path(cfg.output_dir) / f"{cfg.name}-{cfg.fingerprint()}"


def run_eval(cfg: RunConfig, samples: list[Sample], agent: AgentHandle, user: UserHandle, retriever) -> dict:
    out = run_dir(cfg)
    out.mkdir(parents=True, exist_ok=True)
    save_config(cfg, out / "config.yaml")
    (out / "fingerprint.txt").write_text(cfg.fingerprint() + "\n")
    sess_path = out / "sessions.jsonl"
    done = _read_done(sess_path)
    todo = [s for s in samples if s.qid not in done]
    if done:
        log.info("resuming %s: %d done, %d to go", out, len(done), len(todo))

    write_lock = threading.Lock()
    with open(sess_path, "a") as sess_f, open(out / "errors.jsonl", "a") as err_f:
        def work(s: Sample):
            try:
                rec = run_session(s, agent, user, retriever, cfg.eval.max_rounds, cfg.name)
                with write_lock:
                    sess_f.write(json.dumps(rec) + "\n")
                    sess_f.flush()
                    done[s.qid] = rec
            except Exception as e:  # keep the run going; failures are logged and retried on resume
                with write_lock:
                    err_f.write(json.dumps({"qid": s.qid, "error": repr(e),
                                            "trace": traceback.format_exc()}) + "\n")
                    err_f.flush()
                log.error("session %s failed: %r", s.qid, e)

        with ThreadPoolExecutor(max_workers=max(1, cfg.eval.workers)) as ex:
            futs = [ex.submit(work, s) for s in todo]
            for _ in tqdm(as_completed(futs), total=len(futs), desc=cfg.name):
                pass

    records = [done[s.qid] for s in samples if s.qid in done]
    return finalize(cfg, records, out)


def finalize(cfg: RunConfig, records: list[dict], out: Path) -> dict:
    if not records:
        raise RuntimeError(f"no successful sessions in {out}; see errors.jsonl")
    ranks = np.array([r["ranks"] for r in records])
    m = compute_metrics(ranks, cfg.eval.ks)
    cost = Counter()
    for r in records:
        cost.update(r.get("cost") or {})
    rounds = max(1, cfg.eval.max_rounds)
    m["cost_per_round"] = {k: round(v / len(records) / rounds, 3) for k, v in sorted(cost.items())}
    m["fingerprint"] = cfg.fingerprint()
    m["name"] = cfg.name
    (out / "metrics.json").write_text(json.dumps(m, indent=2))
    table = format_table(m)
    (out / "metrics.txt").write_text(table + "\n")
    if cfg.eval.export_plugir_format:
        dialogs = [{"img": r["target"], "dialog": [r["caption"]] + [f"{x['question']} {x['answer']}" for x in r["rounds"]]}
                   for r in records]
        recon = [{"img": r["target"], "dialog": r["queries"]} for r in records]
        (out / "plugir_dialogs.json").write_text(json.dumps(dialogs))
        (out / "plugir_reconed.json").write_text(json.dumps(recon))
    print(table)
    if m["cost_per_round"]:
        print("cost per round:", m["cost_per_round"])
    return m


# ----------------------------------------------------------------------------- fixed dialogs
def eval_dialog_file(cfg: RunConfig, path: str, retriever, split: bool, name: str | None = None) -> dict:
    """Score an existing dialog file (PlugIR/ChatIR format) without any LLM calls.

    split=True: dialog[r] is already the full query for round r (PlugIR's *_reconed.json).
    split=False: query for round r is ", ".join(dialog[:r+1]) (ChatIR / human VisDial dialogs).
    """
    with open(path) as f:
        dialogs = json.load(f)
    R = cfg.eval.max_rounds
    ranks = []
    for d in tqdm(dialogs, desc=Path(path).name):
        qs = d["dialog"][: R + 1] if split else [", ".join(d["dialog"][: r + 1]) for r in range(R + 1)]
        ranks.append(retriever.ranks_of(qs, retriever.index.index_of(d["img"])))
    m = compute_metrics(np.array(ranks), cfg.eval.ks)
    m["source"] = path
    out = Path(cfg.output_dir) / (name or f"dialogs-{Path(path).stem}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(m, indent=2))
    print(format_table(m))
    return m
