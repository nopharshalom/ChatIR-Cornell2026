"""Dialog -> retrieval query.

* `ConcatReformulator`: ChatIR style, caption and Q&A rounds joined with ", ".
* `PlugIRReformulator`: LLM rewrites caption + dialog into one caption (PlugIR's
  "context reformulation"), paraphrasing if the rewrite did not change.
"""
from __future__ import annotations

from ..config import ReformulatorConfig
from . import prompts as P
from .state import DialogState


class ConcatReformulator:
    def __init__(self, cfg: ReformulatorConfig):
        self.sep = cfg.sep

    def reformulate(self, state: DialogState, llm) -> str:
        return self.sep.join(state.dialog_list())


class PlugIRReformulator:
    def __init__(self, cfg: ReformulatorConfig):
        self.cfg = cfg

    def reformulate(self, state: DialogState, llm) -> str:
        q = llm.complete(P.plugir_reconstruct(state.caption, state.turns), temperature=0.0,
                         max_tokens=512, tag="reformulate")
        if self.cfg.paraphrase_on_repeat and q == state.query_history[-1]:
            q = llm.complete(P.plugir_paraphrase(q), temperature=0.7, max_tokens=512, tag="paraphrase",
                             seed_offset=state.round)
        return q or state.query


def build_reformulator(cfg: ReformulatorConfig):
    if cfg.kind == "concat":
        return ConcatReformulator(cfg)
    if cfg.kind == "plugir":
        return PlugIRReformulator(cfg)
    raise ValueError(cfg.kind)
