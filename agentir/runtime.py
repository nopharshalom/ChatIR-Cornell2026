"""Build the components a config describes (shared by the CLI and both services)."""
from __future__ import annotations

import logging

from .config import LLMConfig, RunConfig

log = logging.getLogger(__name__)


def load_retriever(cfg: RunConfig):
    from .retrieval import CorpusIndex, Retriever, build_encoder

    from pathlib import Path

    if not Path(cfg.retriever.index_path).exists():
        raise SystemExit(f"index not found: {cfg.retriever.index_path}. Run `agentir prepare-data` "
                         f"then `agentir build-index` (or copy a teammate's index there).")
    enc = build_encoder(cfg.retriever)
    index = CorpusIndex.load(cfg.retriever.index_path, device=enc.device)
    log.info("loaded index %s (%d images) on %s", cfg.retriever.index_path, len(index), enc.device)
    return Retriever(enc, index, cfg.retriever.text_cache_size)


def make_llm(cfg: LLMConfig):
    from .llm import LLMClient

    return LLMClient(cfg)


def build_agent(cfg: RunConfig, retriever=None, llm=None):
    from .agent import Agent
    from .retrieval import SearchTool

    retriever = retriever or load_retriever(cfg)
    llm = llm or make_llm(cfg.agent.llm)
    return Agent(cfg.agent, llm, SearchTool(retriever), seed=cfg.seed)


def build_user_sim(cfg: RunConfig, agent_llm=None):
    from .user_sim import build_user_sim as _build

    if cfg.user_sim.kind == "vlm" and cfg.user_sim.llm is None and agent_llm is None:
        agent_llm = make_llm(cfg.agent.llm)
    return _build(cfg.user_sim, agent_llm=agent_llm, llm_factory=make_llm)
