"""Candidate-question generators.

`PlugIRGenerator` reproduces PlugIR's generate_dialog.py loop, including its quirks
(flagged inline) so the baseline numbers are comparable to the paper. Improvement
direction #2 from the kickoff meeting (smarter candidate generation) plugs in here as
a new class implementing `generate`.
"""
from __future__ import annotations

from ..config import GeneratorConfig
from ..retrieval import SearchTool
from . import prompts as P
from .context import format_candidates, related_captions
from .state import DialogState

FALLBACK_QUESTION = "what is the other object in the image?"  # PlugIR's hard-coded fallback


class PlainGenerator:
    """ChatIR-style: questions conditioned only on the caption + dialog."""

    def __init__(self, cfg: GeneratorConfig):
        self.cfg = cfg

    def generate(self, state: DialogState, llm, search: SearchTool) -> list[str]:
        outs = llm.chat(P.plugir_generate_plain(state.caption, state.turns), n=self.cfg.num_candidates,
                        temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                        tag="generate", seed_offset=1000 * state.round)
        return [P.first_question(o) for o in outs]


class PlugIRGenerator:
    def __init__(self, cfg: GeneratorConfig, seed: int = 0):
        self.cfg = cfg
        self.seed = seed

    def _referring(self, state, llm, cand_text, prior, offset) -> str:
        out = llm.complete(P.plugir_generate_referring(state.caption, state.turns, cand_text, prior),
                           temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                           tag="generate", seed_offset=offset)
        return P.first_question(out)

    def _answerable(self, state, llm, question) -> bool:
        ans = llm.complete(P.plugir_filter(state.query, question), temperature=0.0, max_tokens=10, tag="filter")
        return "uncertain" not in ans.lower()

    def generate(self, state: DialogState, llm, search: SearchTool) -> list[str]:
        c = self.cfg
        base = 1000 * state.round
        if not c.referring:
            return PlainGenerator(c).generate(state, llm, search)

        caps, _ = related_captions(state, search, c.thres_low, c.n_clusters, seed=self.seed + state.round)
        state.scratch["context_captions"] = caps
        cand_text = format_candidates(caps)

        questions: list[str] = []
        rejected: list[str] = []  # QUIRK: PlugIR never resets this between candidates
        for k in range(c.num_candidates):
            if not c.filtering:
                questions.append(self._referring(state, llm, cand_text, questions + rejected, base + k))
                continue
            ques = ""
            for t in range(c.filter_retries):
                ques = self._referring(state, llm, cand_text, questions + rejected, base + 10 * k + t)
                if self._answerable(state, llm, ques):
                    rejected.append(ques)  # already answerable from context -> redundant, retry
                else:
                    break
            # QUIRK: PlugIR checks `len(ques_prior) == 3`, so the fallback only fires the first
            # time the rejected list reaches exactly `filter_retries` items.
            if len(rejected) == c.filter_retries:
                out = llm.complete(P.plugir_generate_plain(state.caption, state.turns),
                                   temperature=c.temperature, max_tokens=c.max_tokens,
                                   tag="generate", seed_offset=base + 500 + k)
                ques = P.first_question(out)
                if self._answerable(state, llm, ques):
                    ques = FALLBACK_QUESTION
            questions.append(ques)
        state.scratch["rejected_questions"] = rejected
        return questions


def build_generator(cfg: GeneratorConfig, seed: int = 0):
    if cfg.kind == "plain":
        return PlainGenerator(cfg)
    if cfg.kind == "plugir":
        return PlugIRGenerator(cfg, seed)
    raise ValueError(cfg.kind)
