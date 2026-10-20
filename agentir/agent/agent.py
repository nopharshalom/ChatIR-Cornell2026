"""The questioning agent: generator -> ranker -> (user answers) -> reformulator.

The agent only ever sees text plus its metered `SearchTool`; it never sees the target
image or the target's rank. That boundary is what the HTTP agent service exposes, so
the ping-pong setup and the in-process setup behave identically.
"""
from __future__ import annotations

from collections import Counter

from ..config import AgentConfig
from ..llm import MeteredLLM
from ..retrieval import SearchTool
from .generators import build_generator
from .rankers import build_ranker
from .reformulators import build_reformulator
from .state import DialogState, QuestionDecision, Turn


class Agent:
    def __init__(self, cfg: AgentConfig, llm, search: SearchTool, seed: int = 0):
        self.cfg = cfg
        self.llm = llm
        self.search = search
        self.generator = build_generator(cfg.generator, seed)
        self.ranker = build_ranker(cfg.ranker, cfg.reformulator, seed)
        self.reformulator = build_reformulator(cfg.reformulator)

    def start(self, caption: str) -> DialogState:
        return DialogState(caption=caption)

    def _tools(self, state: DialogState):
        return MeteredLLM(self.llm, state.cost), self.search.with_counter(state.cost)

    def ask(self, state: DialogState) -> QuestionDecision:
        llm, search = self._tools(state)
        state.scratch = {}
        before = Counter(state.cost)
        candidates = self.generator.generate(state, llm, search)
        decision = self.ranker.select(state, candidates, llm, search)
        decision.debug["context_captions"] = state.scratch.get("context_captions")
        decision.debug["cost"] = dict(state.cost - before)
        return decision

    def observe(self, state: DialogState, question: str, answer: str) -> str:
        llm, _ = self._tools(state)
        state.turns.append(Turn(question.strip(), answer.strip()))
        state.query = self.reformulator.reformulate(state, llm)
        state.query_history.append(state.query)
        return state.query
