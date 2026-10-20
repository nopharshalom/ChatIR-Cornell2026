"""Typed run configuration.

Configs are YAML files merged left-to-right (later files win), then `--set a.b=c`
overrides are applied. The fully resolved config is written into every run
directory together with a fingerprint of the settings that affect results, so two
teammates can tell at a glance whether their numbers are comparable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Base):
    """Any OpenAI-compatible chat endpoint: vLLM, SGLang, Ollama, LM Studio, OpenAI, ..."""

    base_url: str = "http://localhost:8000/v1"
    model: str = "Qwen/Qwen3.5-9B"
    api_key: str = "EMPTY"
    api_key_env: Optional[str] = None  # if set, read the key from this env var instead
    temperature: float = 0.0
    max_tokens: int = 256
    timeout: float = 120.0
    max_retries: int = 5
    seed: Optional[int] = 1021
    # Qwen3.5 thinks by default; for short questions/answers that is slow and noisy.
    disable_thinking: bool = True
    # Passed verbatim to the server (backend-specific knobs live here).
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Max side (px) of images sent to a VLM. Smaller = fewer image tokens = faster.
    image_max_side: int = 768
    # SQLite cache of LLM calls; makes reruns free and results replayable. null to disable.
    cache_path: Optional[str] = "cache/llm_cache.sqlite"


class DataConfig(_Base):
    corpus_dir: str = "data"  # contains unlabeled2017/<id>.jpg (same as eval.py --data-dir)
    search_space: str = "Protocol/Search_Space_val_50k.json"
    captions: str = "Protocol/visdial_captions.json"
    queries: str = "dialogues/VisDial_v1.0_queries_val.json"
    start: int = 0
    limit: Optional[int] = None  # evaluate only the first `limit` dialogs after `start`
    subset_file: Optional[str] = None  # optional JSON list of query indices (e.g. a fixed dev split)


class RetrieverConfig(_Base):
    backend: Literal["blip", "clip", "fake"] = "blip"
    model_name: str = "Salesforce/blip-itm-large-coco"
    finetuned_ckpt: Optional[str] = None  # e.g. ChatIR's fine-tuned BLIP text encoder
    device: str = "auto"  # auto | cpu | cuda | cuda:0 | mps
    index_path: str = "cache/index_blip.pt"
    batch_size: int = 64
    # Colab has 2 CPUs; more DataLoader workers than cores stalls image embedding there.
    num_workers: int = Field(default_factory=lambda: min(4, os.cpu_count() or 1))
    text_cache_size: int = 200_000


class UserSimConfig(_Base):
    kind: Literal["vlm", "blip2", "scripted"] = "vlm"
    # null => reuse the agent's LLM endpoint (one Qwen3.5 server can play both roles)
    llm: Optional[LLMConfig] = None
    # PlugIR feeds BLIP-2 "Question: {caption}. {question} Answer:". Off by default for VLMs.
    include_caption: bool = False
    max_tokens: int = 32
    blip2_model: str = "Salesforce/blip2-flan-t5-xl"
    device: str = "auto"


class GeneratorConfig(_Base):
    kind: Literal["plugir", "plain"] = "plugir"
    num_candidates: int = 5  # PlugIR's q_n
    referring: bool = True  # condition on retrieval-candidate captions (PlugIR RCE)
    filtering: bool = True  # drop questions already answerable from the context
    filter_retries: int = 3
    n_clusters: int = 10
    thres_low: int = 500  # candidate-set size at round 1, shrinks linearly per round
    temperature: float = 0.5
    max_tokens: int = 32


class RankerConfig(_Base):
    kind: Literal["first", "random", "plugir_kl", "answer_divergence"] = "plugir_kl"
    # --- PlugIR KL ranker ---
    recall_hitting: int = 10
    thres_low: int = 500
    # --- answer-divergence ranker (ours) ---
    num_answers: int = 3
    ground_answers_in_candidates: bool = True  # show candidate captions when proposing answers
    hypothetical_reformulation: Literal["concat", "llm"] = "concat"
    # jsd: information gain of the answer over a softmax on the top `pool_size` images (graded).
    # jaccard: 1 - overlap of top-k sets (the literal "messiest result set"; saturates at 1.0
    #          once every answer path reshuffles the top-k, so it often can't break ties).
    divergence: Literal["jaccard", "jsd"] = "jsd"
    top_k: int = 50
    pool_size: int = 1000
    jsd_temperature: float = 0.05  # cosine sims live in ~[0.2, 0.6]; T=1 would make every softmax ~uniform
    answer_temperature: float = 0.7


class ReformulatorConfig(_Base):
    kind: Literal["plugir", "concat"] = "plugir"
    paraphrase_on_repeat: bool = True
    sep: str = ", "


class AgentConfig(_Base):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    generator: GeneratorConfig = Field(default_factory=GeneratorConfig)
    ranker: RankerConfig = Field(default_factory=RankerConfig)
    reformulator: ReformulatorConfig = Field(default_factory=ReformulatorConfig)


class EvalConfig(_Base):
    max_rounds: int = 10
    ks: list[int] = Field(default_factory=lambda: [1, 10, 50])
    workers: int = 1  # concurrent sessions (helps a lot with vLLM; keep 1 on a laptop)
    export_plugir_format: bool = True


class RunConfig(_Base):
    name: str = "dev"
    seed: int = 1021
    output_dir: str = "runs"
    mode: Literal["local", "http"] = "local"
    agent_url: str = "http://localhost:7001"
    user_url: str = "http://localhost:7002"
    data: DataConfig = Field(default_factory=DataConfig)
    retriever: RetrieverConfig = Field(default_factory=RetrieverConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    user_sim: UserSimConfig = Field(default_factory=UserSimConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)

    def fingerprint(self) -> str:
        """Hash of every setting that can change results (excludes paths, URLs, names)."""
        d = self.model_dump()
        for k in ("name", "output_dir", "mode", "agent_url", "user_url"):
            d.pop(k, None)
        d["eval"].pop("workers", None)
        d["eval"].pop("export_plugir_format", None)
        for llm in (d["agent"]["llm"], (d["user_sim"] or {}).get("llm") or {}):
            for k in ("base_url", "api_key", "api_key_env", "timeout", "max_retries", "cache_path"):
                llm.pop(k, None)
        d["retriever"] = {k: v for k, v in d["retriever"].items()
                          if k in ("backend", "model_name", "finetuned_ckpt")}
        d["data"] = {k: v for k, v in d["data"].items() if k in ("start", "limit", "subset_file")}
        blob = json.dumps(d, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


# ----------------------------------------------------------------------------- loading

# Backend-specific request knobs: a later file replaces them wholesale (never merged), so
# switching backend can't leak e.g. Ollama's reasoning_effort into a vLLM request.
_ATOMIC_KEYS = {"extra_body"}


def _deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and k not in _ATOMIC_KEYS and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _set_dotted(d: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
        if cur is None:
            raise ValueError(f"cannot set {dotted}: '{k}' is null")
    cur[keys[-1]] = value


def load_config(paths: list[str] | None = None, overrides: list[str] | None = None) -> RunConfig:
    merged: dict = {}
    for p in paths or []:
        with open(p) as f:
            merged = _deep_merge(merged, yaml.safe_load(f) or {})
    for ov in overrides or []:
        if "=" not in ov:
            raise ValueError(f"override must look like key.path=value, got {ov!r}")
        key, raw = ov.split("=", 1)
        _set_dotted(merged, key.strip(), yaml.safe_load(raw))
    return RunConfig.model_validate(merged)


def save_config(cfg: RunConfig, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg.model_dump(), f, sort_keys=False)
