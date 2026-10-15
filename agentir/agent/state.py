from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    question: str
    answer: str

    def as_text(self) -> str:
        """PlugIR stores each round as '<question> <answer>' (question ends with '?')."""
        return f"{self.question} {self.answer}"


@dataclass
class DialogState:
    caption: str
    turns: list[Turn] = field(default_factory=list)
    query: str = ""  # current retrieval query (reformulated dialog)
    query_history: list[str] = field(default_factory=list)
    cost: Counter = field(default_factory=Counter)  # LLM calls + probe searches, cumulative
    scratch: dict[str, Any] = field(default_factory=dict)  # per-round data shared between components

    def __post_init__(self):
        if not self.query:
            self.query = self.caption
        if not self.query_history:
            self.query_history = [self.query]

    @property
    def round(self) -> int:
        """1-based index of the round about to be asked."""
        return len(self.turns) + 1

    def dialog_list(self) -> list[str]:
        """[caption, 'q1 a1', 'q2 a2', ...] - the format PlugIR prompts are built from."""
        return [self.caption] + [t.as_text() for t in self.turns]


@dataclass
class QuestionDecision:
    question: str
    candidates: list[str]
    scores: list[float] | None = None
    debug: dict[str, Any] = field(default_factory=dict)
