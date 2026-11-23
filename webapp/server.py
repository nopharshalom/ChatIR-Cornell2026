"""Human-in-the-loop web demo for interactive text-to-image retrieval.

    python -m webapp.server --demo                                     # offline, no models
    python -m webapp.server -c configs/base.yaml -c configs/backends/ollama.yaml \
                            -c configs/experiments/plugir.yaml          # real PlugIR baseline

Every session (each question, who wrote it, the answer, the reformulated query, the
results and, in challenge mode, the target rank) is appended to
`runs/webapp/contributions.jsonl`; `GET /api/contributions.json` exports the finished
dialogs in PlugIR/ChatIR format so `agentir eval-dialogs` can score them.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .backends import InteractiveBackend

log = logging.getLogger("webapp")
STATIC = Path(__file__).parent / "static"


# ----------------------------------------------------------------------------- schemas
class StartReq(BaseModel):
    caption: str = Field(min_length=1, max_length=500)
    target: Optional[str] = None  # challenge mode: corpus path of the image to find
    k: int = Field(12, ge=1, le=60)
    participant: Optional[str] = Field(None, max_length=80)


class TurnReq(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=300)
    # agent: the agent's chosen question; alternative: another candidate it generated;
    # user: a question the person wrote themselves
    source: Literal["agent", "alternative", "user"] = "agent"
    answered_by: Literal["human", "simulator"] = "human"


class SimReq(BaseModel):
    question: str = Field(min_length=1, max_length=300)


class FinishReq(BaseModel):
    found: Optional[str] = None  # corpus path the person picked as their image
    rating: Optional[int] = Field(None, ge=1, le=5)
    note: Optional[str] = Field(None, max_length=1000)


@dataclass
class Session:
    sid: str
    caption: str
    state: Any
    k: int
    target: Optional[str] = None
    participant: Optional[str] = None
    turns: list[dict] = field(default_factory=list)
    snapshots: list[Any] = field(default_factory=list)
    pending: Optional[dict] = None  # last ask() result not yet answered
    rank_history: list[Optional[int]] = field(default_factory=list)
    done: bool = False
    outcome: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ContributionLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: str, sess: Session, **data):
        rec = {"event": event, "time": time.time(), "session_id": sess.sid,
               "participant": sess.participant, **data}
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def finished(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock, open(self.path, encoding="utf-8") as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
        return [r for r in recs if r["event"] == "finish"]


# ----------------------------------------------------------------------------- app
def create_app(backend: InteractiveBackend, log_path: str | Path = "runs/webapp/contributions.jsonl"):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, Response
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Interactive retrieval demo")
    sessions: dict[str, Session] = {}
    contrib = ContributionLog(Path(log_path))
    rng = random.Random()
    info = backend.info()

    def get(sid: str) -> Session:
        s = sessions.get(sid)
        if s is None:
            raise HTTPException(404, "unknown session (the server may have restarted)")
        return s

    def view(s: Session) -> dict:
        return {"session_id": s.sid, "caption": s.caption, "round": len(s.turns) + 1,
                "max_rounds": info["max_rounds"], "query": s.state.query,
                "query_history": list(s.state.query_history), "turns": s.turns,
                "results": backend.results(s.state, s.k), "target": s.target,
                "target_rank": s.rank_history[-1] if s.rank_history else None,
                "rank_history": s.rank_history, "pending": s.pending, "done": s.done,
                "cost": {k: int(v) for k, v in s.state.cost.items()}}

    def rank(s: Session):
        if s.target:
            s.rank_history.append(backend.target_rank(s.state, s.target))

    @app.get("/api/info")
    def get_info():
        return {**info, "sessions": len(sessions)}

    @app.get("/api/challenge")
    def challenge():
        c = backend.challenge(rng)
        if c is None:
            raise HTTPException(404, "challenge mode needs the VisDial queries and their targets")
        return c

    @app.post("/api/sessions")
    def start(req: StartReq):
        st = backend.start(req.caption.strip())
        s = Session(uuid.uuid4().hex[:12], req.caption.strip(), st, req.k, req.target, req.participant)
        rank(s)
        sessions[s.sid] = s
        contrib.write("start", s, caption=s.caption, target=s.target, backend=info["backend"],
                      fingerprint=info["fingerprint"], target_rank=s.rank_history[-1] if s.rank_history else None)
        return view(s)

    @app.get("/api/sessions/{sid}")
    def show(sid: str):
        return view(get(sid))

    @app.post("/api/sessions/{sid}/ask")
    def ask(sid: str):
        s = get(sid)
        with s.lock:
            if s.done:
                raise HTTPException(409, "session finished")
            t0 = time.time()
            s.pending = backend.ask(s.state)
            s.pending["seconds"] = round(time.time() - t0, 2)
            contrib.write("ask", s, round=len(s.turns) + 1, **s.pending)
            return s.pending

    @app.post("/api/sessions/{sid}/simulate")
    def simulate(sid: str, req: SimReq):
        s = get(sid)
        if not s.target:
            raise HTTPException(400, "the simulated user needs a target image (challenge mode)")
        ans = backend.simulate_answer(s.state, s.target, req.question)
        if ans is None:
            raise HTTPException(501, "no simulated user available for this image")
        return {"answer": ans}

    @app.post("/api/sessions/{sid}/turn")
    def turn(sid: str, req: TurnReq):
        s = get(sid)
        with s.lock:
            if s.done:
                raise HTTPException(409, "session finished")
            s.snapshots.append((copy.deepcopy(s.state), s.pending, list(s.rank_history)))
            prev_query = s.state.query
            query = backend.observe(s.state, req.question.strip(), req.answer.strip())
            rank(s)
            rec = {"round": len(s.turns) + 1, "question": req.question.strip(), "answer": req.answer.strip(),
                   "source": req.source, "answered_by": req.answered_by, "query_before": prev_query,
                   "query": query, "agent_question": (s.pending or {}).get("question"),
                   "target_rank": s.rank_history[-1] if s.rank_history else None}
            s.turns.append(rec)
            s.pending = None
            contrib.write("turn", s, **rec)
            return view(s)

    @app.post("/api/sessions/{sid}/undo")
    def undo(sid: str):
        s = get(sid)
        with s.lock:
            if not s.snapshots or s.done:
                raise HTTPException(409, "nothing to undo")
            s.state, s.pending, s.rank_history = s.snapshots.pop()
            removed = s.turns.pop()
            contrib.write("undo", s, round=removed["round"])
            return view(s)

    @app.post("/api/sessions/{sid}/finish")
    def finish(sid: str, req: FinishReq):
        s = get(sid)
        with s.lock:
            s.done = True
            s.outcome = req.model_dump()
            found_rank = None
            if req.found:
                found_rank = next((r["rank"] for r in backend.results(s.state, s.k) if r["path"] == req.found), None)
            contrib.write("finish", s, caption=s.caption, target=s.target, found=req.found,
                          found_rank=found_rank, rating=req.rating, note=req.note,
                          success=bool(s.target and req.found == s.target),
                          dialog=[s.caption] + [f"{t['question']} {t['answer']}" for t in s.turns],
                          queries=list(s.state.query_history), rank_history=s.rank_history,
                          sources=[t["source"] for t in s.turns], backend=info["backend"],
                          fingerprint=info["fingerprint"])
            return view(s)

    @app.get("/api/contributions.json")
    def contributions():
        """Finished dialogs in ChatIR/PlugIR format ({img, dialog}); img is the target or the image picked."""
        out = []
        for r in contrib.finished():
            img = r.get("target") or r.get("found")
            if img:
                out.append({"img": img, "dialog": r["dialog"], "session_id": r["session_id"],
                            "source": "challenge" if r.get("target") else "free"})
        return out

    @app.get("/api/collage")
    def collage(n: int = 24):
        return backend.collage(rng, max(1, min(n, 60)))

    @app.get("/images/{path:path}")
    def image(path: str):
        kind, val = backend.image(path)
        if kind == "svg":
            return Response(val, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})
        if kind == "file":
            return FileResponse(val, headers={"Cache-Control": "max-age=86400"})
        raise HTTPException(404, "image not available")

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", action="append", default=[], help="agentir YAML configs, merged in order")
    ap.add_argument("--set", action="append", default=[], help="override, e.g. agent.ranker.kind=answer_divergence")
    ap.add_argument("--demo", action="store_true", help="synthetic world + mock LLM (no models, no downloads)")
    ap.add_argument("--demo-ranker", default="plugir_kl", choices=["plugir_kl", "answer_divergence", "random", "first"])
    ap.add_argument("--no-user-sim", action="store_true", help="disable 'let the AI answer' in challenge mode")
    ap.add_argument("--log", default="runs/webapp/contributions.jsonl")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .backends import build_agentir_backend, build_demo_backend

    if args.demo:
        backend = build_demo_backend(args.demo_ranker)
    else:
        if not args.config:
            ap.error("pass agentir configs with -c (e.g. -c configs/base.yaml -c configs/experiments/plugir.yaml) or use --demo")
        from agentir.config import load_config

        backend = build_agentir_backend(load_config(args.config, args.set), with_user_sim=not args.no_user_sim)

    import uvicorn

    log.info("backend %s ready; open http://%s:%d", backend.name, args.host, args.port)
    uvicorn.run(create_app(backend, args.log), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
