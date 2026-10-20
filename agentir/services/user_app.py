"""User-simulation service: owns the target images and the answering VLM."""
from __future__ import annotations

from pathlib import Path

from ..config import RunConfig
from .schemas import AnswerRequest, AnswerResponse


def create_app(cfg: RunConfig, user_sim=None):
    from fastapi import FastAPI, HTTPException

    from ..runtime import build_user_sim

    user_sim = user_sim or build_user_sim(cfg)
    root = Path(cfg.data.corpus_dir).resolve()
    app = FastAPI(title="AgentIR user-simulation service")

    @app.get("/health")
    def health():
        return {"ok": True, "kind": cfg.user_sim.kind}

    @app.post("/answer", response_model=AnswerResponse)
    def answer(req: AnswerRequest):
        path = (root / req.target).resolve()
        if root not in path.parents or not path.exists():
            raise HTTPException(400, f"target not found under corpus_dir: {req.target}")
        return AnswerResponse(answer=user_sim.answer(str(path), req.question, req.context))

    return app
