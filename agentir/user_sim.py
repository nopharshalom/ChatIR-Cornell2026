"""Simulated users: answer the agent's question while looking at the target image.

* `VLMUserSimulator` - any OpenAI-compatible VLM (default: Qwen3.5, natively multimodal).
* `Blip2UserSimulator` - PlugIR's original BLIP-2 FlanT5-XL answerer, for exact replication.
* `ScriptedUserSimulator` - answers from a lookup function; used by tests/smoke runs.
"""
from __future__ import annotations

from typing import Callable, Optional

from .agent.prompts import user_sim_messages
from .config import UserSimConfig
from .llm import user_message_with_image


class VLMUserSimulator:
    def __init__(self, cfg: UserSimConfig, llm):
        self.cfg = cfg
        self.llm = llm

    def answer(self, image_path: str, question: str, caption: Optional[str] = None) -> str:
        sys, text = user_sim_messages(question, caption if self.cfg.include_caption else None)
        max_side = getattr(getattr(self.llm, "cfg", None), "image_max_side", 768)
        msgs = [{"role": "system", "content": sys}, user_message_with_image(text, image_path, max_side)]
        return self.llm.complete(msgs, temperature=0.0, max_tokens=self.cfg.max_tokens, tag="user_answer").strip()


class Blip2UserSimulator:
    def __init__(self, cfg: UserSimConfig):
        import threading

        import torch
        from transformers import Blip2ForConditionalGeneration, Blip2Processor

        from .retrieval import resolve_device

        self.cfg = cfg
        self.device = resolve_device(cfg.device)
        dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        self.processor = Blip2Processor.from_pretrained(cfg.blip2_model)
        try:
            model = Blip2ForConditionalGeneration.from_pretrained(cfg.blip2_model, dtype=dtype)
        except TypeError:  # transformers < 4.56
            model = Blip2ForConditionalGeneration.from_pretrained(cfg.blip2_model, torch_dtype=dtype)
        self.model = model.to(self.device).eval()
        self._lock = threading.Lock()

    def answer(self, image_path: str, question: str, caption: Optional[str] = None) -> str:
        import torch
        from PIL import Image

        # PlugIR: query = caption_recon + '. ' + question  ->  "Question: {query} Answer:"
        query = f"{caption}. {question}" if (caption and self.cfg.include_caption) else question
        inputs = self.processor(images=Image.open(image_path).convert("RGB"),
                                text=f"Question: {query} Answer:", return_tensors="pt").to(self.device)
        with self._lock, torch.no_grad():
            out = self.model.generate(**inputs, do_sample=False)
        return self.processor.decode(out[0], skip_special_tokens=True).strip()


class ScriptedUserSimulator:
    def __init__(self, fn: Callable[[str, str], str]):
        self.fn = fn

    def answer(self, image_path: str, question: str, caption: Optional[str] = None) -> str:
        return self.fn(image_path, question)


def build_user_sim(cfg: UserSimConfig, agent_llm=None, llm_factory=None):
    if cfg.kind == "vlm":
        llm = agent_llm if cfg.llm is None else llm_factory(cfg.llm)
        return VLMUserSimulator(cfg, llm)
    if cfg.kind == "blip2":
        return Blip2UserSimulator(cfg)
    raise ValueError(f"user_sim.kind={cfg.kind!r} must be constructed programmatically")
