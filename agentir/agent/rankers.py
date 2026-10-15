"""Question rankers: pick one question out of the generated candidates.

* `PlugIRKLRanker` - PlugIR's selection (paper Alg. 1 line 10, `select_question`):
  embed `query + ", " + question` (no answer!), compare the softmax over candidates
  ranked k..n before/after, keep the question with the *minimum* KL divergence.
* `AnswerDivergenceRanker` - the kickoff-meeting hypothesis: for each candidate, have
  the LLM propose plausible answers, run a probe search for every answer path, and
  keep the question whose answer paths produce the most different result sets.
"""
from __future__ import annotations

import itertools
import json
import random
import re

import torch

from ..config import RankerConfig, ReformulatorConfig
from ..retrieval import SearchTool
from . import prompts as P
from .context import candidate_set_size
from .state import DialogState, QuestionDecision, Turn


class FirstRanker:
    def select(self, state, candidates, llm, search) -> QuestionDecision:
        return QuestionDecision(candidates[0], candidates)


class RandomRanker:
    def __init__(self, seed: int = 0):
        self.seed = seed

    def select(self, state, candidates, llm, search) -> QuestionDecision:
        rng = random.Random(f"{self.seed}:{state.caption}:{state.round}")
        return QuestionDecision(rng.choice(candidates), candidates)


class PlugIRKLRanker:
    def __init__(self, cfg: RankerConfig):
        self.cfg = cfg

    def select(self, state: DialogState, candidates: list[str], llm, search: SearchTool) -> QuestionDecision:
        n = candidate_set_size(self.cfg.thres_low, state.round)
        k = self.cfg.recall_hitting
        sims = search.similarity([state.query], purpose="rank")[0]
        # PlugIR: cossim.argsort()[-threshold:-k] -> candidates ranked k..n (top-k excluded)
        related = torch.argsort(sims)[-n:-k] if n > k else torch.argsort(sims)[-n:]
        p_prev = torch.softmax(sims[related], dim=0)
        sims_q = search.similarity([f"{state.query}, {q}" for q in candidates], purpose="rank")
        p_q = torch.softmax(sims_q[:, related], dim=1)
        kl = (p_prev * (p_prev.log() - p_q.log())).sum(dim=1)
        best = int(torch.argmin(kl))
        return QuestionDecision(candidates[best], candidates, kl.tolist(), {"criterion": "min_kl"})


def _parse_answers(text: str, m: int) -> list[str]:
    text = text.strip()
    match = re.search(r"\[.*\]", text, re.S)
    if match:
        try:
            vals = json.loads(match.group(0))
            ans = [str(v).strip() for v in vals if str(v).strip()]
            if ans:
                return list(dict.fromkeys(ans))[:m]
        except json.JSONDecodeError:
            pass
    lines = [re.sub(r"^\s*(?:[-*\d.)\]]+\s*)", "", ln).strip(" \"'") for ln in text.splitlines()]
    return list(dict.fromkeys(ln for ln in lines if ln))[:m]


def jaccard_divergence(sims: torch.Tensor, k: int) -> float:
    """1 - mean pairwise Jaccard overlap of the top-k result sets of each answer path."""
    if sims.shape[0] < 2:
        return 0.0
    tops = [set(torch.topk(s, k).indices.tolist()) for s in sims]
    overlaps = [len(a & b) / len(a | b) for a, b in itertools.combinations(tops, 2)]
    return 1.0 - sum(overlaps) / len(overlaps)


def js_divergence(sims: torch.Tensor, pool: torch.Tensor, temperature: float) -> float:
    """Generalised JSD of the answer-path result distributions over a candidate pool.

    With a uniform prior over answers this equals I(answer; target image) under the
    retrieval model, i.e. the expected information gain of asking the question.
    """
    if sims.shape[0] < 2:
        return 0.0
    p = torch.softmax(sims[:, pool] / temperature, dim=1)
    ent = lambda q: -(q * q.clamp_min(1e-12).log()).sum(-1)  # noqa: E731
    return float(ent(p.mean(0)) - ent(p).mean())


class AnswerDivergenceRanker:
    def __init__(self, cfg: RankerConfig, reformulator_cfg: ReformulatorConfig | None = None):
        self.cfg = cfg
        self.sep = (reformulator_cfg or ReformulatorConfig()).sep

    def _hypothetical_query(self, state: DialogState, q: str, a: str, llm) -> str:
        if self.cfg.hypothetical_reformulation == "concat":
            return f"{state.query}{self.sep}{q} {a}"
        msgs = P.plugir_reconstruct(state.caption, state.turns + [Turn(q, a)])
        return llm.complete(msgs, temperature=0.0, max_tokens=256, tag="hyp_reformulate")

    def select(self, state: DialogState, candidates: list[str], llm, search: SearchTool) -> QuestionDecision:
        c = self.cfg
        ctx = state.scratch.get("context_captions") if c.ground_answers_in_candidates else None
        base = search.similarity([state.query], purpose="rank")[0]
        pool = torch.topk(base, min(c.pool_size, base.numel())).indices

        scores, details = [], []
        seen: dict[str, int] = {}
        for i, q in enumerate(candidates):
            if q in seen:  # PlugIR's generator often repeats itself; don't pay twice
                scores.append(scores[seen[q]])
                details.append({**details[seen[q]], "duplicate_of": seen[q]})
                continue
            seen[q] = i
            raw = llm.complete(P.agentir_propose_answers(state.caption, state.turns, q, c.num_answers, ctx),
                               temperature=c.answer_temperature, max_tokens=128, tag="propose_answers",
                               seed_offset=1000 * state.round + i)
            answers = _parse_answers(raw, c.num_answers)
            if len(answers) < 2:
                scores.append(float("-inf"))
                details.append({"question": q, "answers": answers, "note": "fewer than 2 answers"})
                continue
            hyp = [self._hypothetical_query(state, q, a, llm) for a in answers]
            sims = search.similarity(hyp, purpose="probe")
            score = (jaccard_divergence(sims, c.top_k) if c.divergence == "jaccard"
                     else js_divergence(sims, pool, c.jsd_temperature))
            scores.append(score)
            details.append({"question": q, "answers": answers, "score": score})

        best = max(range(len(candidates)), key=lambda j: scores[j])  # ties -> earliest candidate
        return QuestionDecision(candidates[best], candidates, scores,
                                {"criterion": f"max_{c.divergence}", "paths": details})


def build_ranker(cfg: RankerConfig, reformulator_cfg: ReformulatorConfig | None = None, seed: int = 0):
    if cfg.kind == "first":
        return FirstRanker()
    if cfg.kind == "random":
        return RandomRanker(seed)
    if cfg.kind == "plugir_kl":
        return PlugIRKLRanker(cfg)
    if cfg.kind == "answer_divergence":
        return AnswerDivergenceRanker(cfg, reformulator_cfg)
    raise ValueError(cfg.kind)
