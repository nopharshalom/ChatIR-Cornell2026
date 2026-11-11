"""Dialog -> retrieval query.

* `ConcatReformulator`: ChatIR style, caption and Q&A rounds joined with ", ".
* `PlugIRReformulator`: LLM rewrites caption + dialog into one caption (PlugIR's
  "context reformulation"), paraphrasing if the rewrite did not change.
"""
from __future__ import annotations

import re

from ..config import ReformulatorConfig
from . import prompts as P
from .state import DialogState


class ConcatReformulator:
    def __init__(self, cfg: ReformulatorConfig):
        self.sep = cfg.sep

    def reformulate(self, state: DialogState, llm) -> str:
        return self.sep.join(state.dialog_list())


class PlugIRReformulator:
    prompt = staticmethod(P.plugir_reconstruct)

    def __init__(self, cfg: ReformulatorConfig):
        self.cfg = cfg

    def reformulate(self, state: DialogState, llm) -> str:
        q = llm.complete(self.prompt(state.caption, state.turns), temperature=0.0,
                         max_tokens=512, tag="reformulate")
        if self.cfg.paraphrase_on_repeat and q == state.query_history[-1]:
            q = llm.complete(P.plugir_paraphrase(q), temperature=0.7, max_tokens=512, tag="paraphrase",
                             seed_offset=state.round)
        return q or state.query


class GroundedReformulator(PlugIRReformulator):
    """E2: same loop as PlugIR, prompt that forbids dropping, inventing or leaking details."""

    prompt = staticmethod(P.agentir_grounded_reconstruct)


# Sentences that talk about the prompt or other images instead of the target, e.g.
# "... with a pig also stuffed into a suitcase among the retrieval candidates".
LEAK_RE = re.compile(r"retrieval candidate|candidates|mentioned|the caption|the dialog|no information|"
                     r"cannot be determined", re.I)


def strip_leaked_sentences(text: str) -> str:
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sents if not LEAK_RE.search(s)]
    if len(kept) == len(sents):
        return text
    if not kept:  # every sentence leaks: drop only the offending comma-separated clauses
        kept = [", ".join(c for c in re.split(r",\s*", x) if not LEAK_RE.search(c)) for x in sents]
        kept = [k for k in kept if k]
    return " ".join(kept)


class PostProcessed:
    """Wraps a reformulator with E2's optional anchoring / leak stripping."""

    def __init__(self, inner, cfg: ReformulatorConfig):
        self.inner, self.cfg = inner, cfg

    def reformulate(self, state: DialogState, llm) -> str:
        q = self.inner.reformulate(state, llm)
        if self.cfg.strip_leaks:
            q = strip_leaked_sentences(q) or state.caption
        if self.cfg.anchor_caption and not q.lower().startswith(state.caption.lower()):
            q = f"{state.caption}. {q}"
        return q


def build_reformulator(cfg: ReformulatorConfig):
    if cfg.kind == "concat":
        r = ConcatReformulator(cfg)
    elif cfg.kind == "plugir":
        r = PlugIRReformulator(cfg)
    elif cfg.kind == "grounded":
        r = GroundedReformulator(cfg)
    else:
        raise ValueError(cfg.kind)
    return PostProcessed(r, cfg) if (cfg.anchor_caption or cfg.strip_leaks) else r
