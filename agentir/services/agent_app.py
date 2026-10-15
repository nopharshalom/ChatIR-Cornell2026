"""Agent service: owns the LLM questioner and a metered search tool. Never sees images."""
from __future__ import annotations

import math
import threading
import uuid

from ..config import RunConfig
from .schemas import AskResponse, ObserveRequest, ObserveResponse, StartRequest, StartResponse


def _finite(xs):
    return None if xs is None else [x if math.isfinite(x) else -1e30 for x in xs]


def create_app(cfg: RunConfig, agent=None):
    from fastapi import FastAPI, HTTPException

    from ..runtime import build_agent

    agent = agent or build_agent(cfg)
    sessions: dict = {}
    lock = threading.Lock()
    app = FastAPI(title="AgentIR agent service")

    def get(sid):
        st = sessions.get(sid)
        if st is None:
            raise HTTPException(404, f"unknown session {sid}")
        return st

    @app.get("/health")
    def health():
        return {"ok": True, "fingerprint": cfg.fingerprint(), "sessions": len(sessions)}

    @app.post("/sessions", response_model=StartResponse)
    def start(req: StartRequest):
        sid = req.session_id or uuid.uuid4().hex
        st = agent.start(req.caption)
        with lock:
            sessions[sid] = st
        return StartResponse(session_id=sid, query=st.query)

    @app.post("/sessions/{sid}/ask", response_model=AskResponse)
    def ask(sid: str):
        d = agent.ask(get(sid))
        return AskResponse(question=d.question, candidates=d.candidates, scores=_finite(d.scores),
                           debug=d.debug)

    @app.post("/sessions/{sid}/observe", response_model=ObserveResponse)
    def observe(sid: str, req: ObserveRequest):
        st = get(sid)
        return ObserveResponse(query=agent.observe(st, req.question, req.answer), cost=dict(st.cost))

    @app.delete("/sessions/{sid}")
    def close(sid: str):
        with lock:
            sessions.pop(sid, None)
        return {"ok": True}

    return app
