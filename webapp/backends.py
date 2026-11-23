"""Retrieval backends the web app can drive.

The web app only talks to `InteractiveBackend`. `AgentIRBackend` wraps any
`agentir.agent.Agent`, so swapping PlugIR for our best method is a config change
(`-c configs/experiments/<method>.yaml`), not a code change. To plug in something that
is not an `Agent` at all, implement the same methods and pass it to `create_app`.

The human replaces the simulated user: the agent asks, the person answers (or writes
their own question), and every answer is folded into the next retrieval query by the
agent's reformulator, exactly as in an offline `agentir run`.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from agentir.agent.state import DialogState

COCO_IMAGE_URL = "http://images.cocodataset.org/{path}"


class InteractiveBackend(Protocol):
    name: str

    def info(self) -> dict[str, Any]: ...
    def start(self, caption: str) -> DialogState: ...
    def results(self, state: DialogState, k: int) -> list[dict[str, Any]]: ...
    def ask(self, state: DialogState) -> dict[str, Any]: ...
    def observe(self, state: DialogState, question: str, answer: str) -> str: ...
    def challenge(self, rng: random.Random) -> Optional[dict[str, Any]]: ...
    def target_rank(self, state: DialogState, target: str) -> Optional[int]: ...
    def simulate_answer(self, state: DialogState, target: str, question: str) -> Optional[str]: ...
    def image(self, path: str) -> tuple[str, Any]: ...  # ("file", Path) | ("redirect", url) | ("svg", str)
    def collage(self, rng: random.Random, n: int) -> list[str]: ...  # image paths for decoration


def _finite(xs):
    return None if xs is None else [float(x) if math.isfinite(x) else None for x in xs]


@dataclass
class AgentIRBackend:
    """Drives an `agentir` Agent with a person in the loop."""

    agent: Any  # agentir.agent.Agent
    retriever: Any  # agentir.retrieval.Retriever (evaluator view: used for display + target rank)
    name: str
    fingerprint: str
    max_rounds: int = 10
    samples: Optional[list] = None  # agentir.data.Sample list, for challenge mode
    user_sim: Any = None  # optional simulated user ("let the AI answer for me")
    corpus_dir: Optional[str] = None
    image_fn: Any = None  # optional path -> svg string (synthetic demo world)
    method: Optional[dict] = None
    collage_paths: Optional[list] = None  # decoration images; default: random corpus images

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "fingerprint": self.fingerprint, "max_rounds": self.max_rounds,
                "corpus_size": len(self.retriever.index), "challenge": bool(self.samples),
                "simulated_user": self.user_sim is not None, "method": self.method or {}}

    def start(self, caption: str) -> DialogState:
        return self.agent.start(caption)

    def results(self, state: DialogState, k: int) -> list[dict[str, Any]]:
        idx, vals = self.retriever.search(state.query, k=min(k, len(self.retriever.index)))
        caps = self.retriever.index.captions
        return [{"rank": r + 1, "path": self.retriever.index.paths[i], "score": round(float(v), 4),
                 "caption": caps[i] if i < len(caps) else ""}
                for r, (i, v) in enumerate(zip(idx.tolist(), vals.tolist()))]

    def ask(self, state: DialogState) -> dict[str, Any]:
        d = self.agent.ask(state)
        dbg = d.debug or {}
        ctx_idx = dbg.get("context_indices")
        ctx_idx = [int(i) for i in ctx_idx.tolist()] if hasattr(ctx_idx, "tolist") else list(ctx_idx or [])
        context = [{"path": self.retriever.index.paths[i], "caption": c}
                   for i, c in zip(ctx_idx, dbg.get("context_captions") or [])]
        # Deduplicate candidates for display, keeping each one's best score.
        cands: dict[str, Optional[float]] = {}
        for q, s in zip(d.candidates, _finite(d.scores) or [None] * len(d.candidates)):
            q = q.strip()
            if q and q not in cands:
                cands[q] = s
        return {"question": d.question.strip(), "criterion": dbg.get("criterion"),
                "candidates": [{"question": q, "score": s} for q, s in cands.items()],
                "context": context, "cost": {k: int(v) for k, v in (dbg.get("cost") or {}).items()}}

    def observe(self, state: DialogState, question: str, answer: str) -> str:
        return self.agent.observe(state, question, answer)

    def challenge(self, rng: random.Random) -> Optional[dict[str, Any]]:
        if not self.samples:
            return None
        s = rng.choice(self.samples)
        return {"target": s.target, "caption": s.caption, "qid": s.qid,
                "human_dialog": s.human_dialog[1:]}

    def target_rank(self, state: DialogState, target: str) -> Optional[int]:
        try:
            ti = self.retriever.index.index_of(target)
        except KeyError:
            return None
        return int(self.retriever.ranks_of([state.query], ti)[0]) + 1  # 1-based

    def simulate_answer(self, state: DialogState, target: str, question: str) -> Optional[str]:
        if self.user_sim is None:
            return None
        if self.image_fn is not None:
            path = target  # synthetic world: the scripted user looks the path up
        else:
            p = Path(self.corpus_dir or ".") / target
            if not p.exists() and not _fetch_coco(target, p):
                return None  # the VLM needs the pixels locally
            path = str(p)
        return self.user_sim.answer(path, question, state.query).strip()

    def collage(self, rng: random.Random, n: int) -> list[str]:
        pool = self.collage_paths or self.retriever.index.paths
        return rng.sample(list(pool), min(n, len(pool)))

    def image(self, path: str):
        if self.image_fn is not None:
            svg = self.image_fn(path)
            if svg is not None:
                return "svg", svg
        if self.corpus_dir:
            root = Path(self.corpus_dir).resolve()
            p = (root / path).resolve()
            if root in p.parents and p.exists():
                return "file", p
        # Images not downloaded locally: every corpus image is a public COCO unlabeled2017 file.
        return "redirect", COCO_IMAGE_URL.format(path=path)


def _fetch_coco(rel: str, dest: Path) -> bool:
    """Download one corpus image from COCO (hosts without the 19 GB image set, e.g. Colab)."""
    import urllib.request

    if not rel.startswith("unlabeled2017/") or ".." in rel:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(COCO_IMAGE_URL.format(path=rel), tmp)
        tmp.replace(dest)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- builders

def build_agentir_backend(cfg, with_user_sim: bool = True) -> AgentIRBackend:
    """Real backend: BLIP index over the 50k corpus + the configured LLM agent."""
    from agentir.data import load_samples
    from agentir.runtime import build_agent, build_user_sim, load_retriever

    retriever = load_retriever(cfg)
    agent = build_agent(cfg, retriever=retriever)
    user_sim = None
    if with_user_sim:
        try:
            user_sim = build_user_sim(cfg, agent_llm=agent.llm)
        except Exception:  # noqa: BLE001 - optional feature
            user_sim = None
    try:
        samples = [s for s in load_samples(cfg.data) if s.target in retriever.index.path_to_idx]
    except FileNotFoundError:
        samples = None
    a = cfg.agent
    return AgentIRBackend(agent=agent, retriever=retriever, name=cfg.name, fingerprint=cfg.fingerprint(),
                          max_rounds=cfg.eval.max_rounds, samples=samples, user_sim=user_sim,
                          corpus_dir=cfg.data.corpus_dir,
                          method={"generator": a.generator.kind, "ranker": a.ranker.kind,
                                  "reformulator": a.reformulator.kind, "llm": a.llm.model,
                                  "retriever": cfg.retriever.model_name})


def build_demo_backend(ranker: str = "plugir_kl") -> AgentIRBackend:
    """Offline demo: agentir's synthetic smoke world + mock LLM. No models, no downloads.

    Runs the real PlugIR generator / KL ranker / reformulator code, so the whole web flow
    can be exercised on any laptop. The "images" are attribute tuples drawn as SVG cards.
    """
    from agentir.agent import Agent
    from agentir.config import RunConfig
    from agentir.data import Sample
    from agentir.retrieval import Retriever, SearchTool
    from agentir.smoke import MockLLM, build_fake_world, scripted_answer
    from agentir.user_sim import ScriptedUserSimulator

    enc, index, queries, lookup = build_fake_world(n_images=600, n_queries=60)
    retriever = Retriever(enc, index)
    cfg = RunConfig.model_validate({
        "name": f"demo-plugir ({ranker})", "retriever": {"backend": "fake"},
        "agent": {"generator": {"thres_low": 200, "n_clusters": 5},
                  "ranker": {"kind": ranker, "top_k": 10, "thres_low": 200},
                  "reformulator": {"kind": "plugir"}},
        "eval": {"max_rounds": 10}})
    agent = Agent(cfg.agent, MockLLM(), SearchTool(retriever), seed=cfg.seed)
    samples = [Sample(i, q["img"], q["dialog"][0], q["dialog"]) for i, q in enumerate(queries)]
    return AgentIRBackend(agent=agent, retriever=retriever, name=cfg.name, fingerprint=cfg.fingerprint(),
                          max_rounds=cfg.eval.max_rounds, samples=samples,
                          user_sim=ScriptedUserSimulator(scripted_answer(lookup)),
                          image_fn=lambda p: demo_svg(lookup[p]) if p in lookup else None,
                          collage_paths=_visdial_targets(),
                          method={"generator": "plugir", "ranker": ranker, "reformulator": "plugir",
                                  "llm": "mock (synthetic world)", "retriever": "hashed bag-of-words"})


def _visdial_targets(path: str = "dialogues/VisDial_v1.0_queries_val.json") -> Optional[list[str]]:
    """Real COCO images for the demo's collage (served from images.cocodataset.org)."""
    import json

    try:
        with open(path) as f:
            return [q["img"] for q in json.load(f)]
    except (OSError, ValueError, KeyError):
        return None


_PLACE_BG = {"park": "#9fd39a", "street": "#b9b9c4", "beach": "#f3dc9b", "kitchen": "#e9c9a8",
             "field": "#c9e08a", "harbor": "#8fc1e3"}
_OBJ = {"dog": "🐕", "cat": "🐈", "car": "🚗", "bus": "🚌", "horse": "🐎", "bike": "🚲",
        "boat": "⛵", "kite": "🪁"}
_COLOR = {"red": "#d93b3b", "blue": "#3b6fd9", "green": "#2f9e4a", "white": "#f4f4f4",
          "black": "#222", "yellow": "#f1c40f"}
_N = {"one": 1, "two": 2, "three": 3}


def demo_svg(a: dict) -> str:
    n = _N[a["count"]]
    xs = {1: [120], 2: [80, 160], 3: [55, 120, 185]}[n]
    items = "".join(
        f'<circle cx="{x}" cy="105" r="30" fill="{_COLOR[a["color"]]}" stroke="#0003" stroke-width="2"/>'
        f'<text x="{x}" y="117" font-size="34" text-anchor="middle">{_OBJ[a["obj"]]}</text>' for x in xs)
    night = '<rect width="240" height="180" fill="#0b1640" opacity="0.55"/><circle cx="205" cy="30" r="12" fill="#f5f3ce"/>' \
        if a["time"] == "night" else '<circle cx="205" cy="30" r="14" fill="#ffd43b"/>'
    label = f'{a["count"]} {a["color"]} {a["obj"]} · {a["place"]} · {a["time"]}'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 180">'
            f'<rect width="240" height="180" fill="{_PLACE_BG[a["place"]]}"/>{night}{items}'
            f'<rect y="152" width="240" height="28" fill="#0007"/>'
            f'<text x="120" y="171" font-size="12" fill="#fff" text-anchor="middle" '
            f'font-family="sans-serif">{label}</text></svg>')
