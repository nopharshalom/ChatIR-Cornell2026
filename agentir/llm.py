"""Thin client for any OpenAI-compatible chat endpoint, with retries and a replay cache.

Every component (questioner, filter, reformulator, answer proposer, VLM user simulator)
goes through `LLMClient.chat`, so swapping Qwen-on-vLLM for Ollama or a paid API is a
config change, and the call counters give a per-round cost breakdown for the paper.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .config import LLMConfig

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_thinking(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # opening tag eaten by the chat template
        text = text.split("</think>", 1)[1]
    return text.strip()


def image_to_data_url(path: str | Path, max_side: int = 768) -> str:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def user_message_with_image(text: str, image_path: str | Path, max_side: int = 768) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": image_to_data_url(image_path, max_side)}},
            {"type": "text", "text": text},
        ],
    }


class _SqliteCache:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[list[str]]:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: list[str]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
            self._db.commit()


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self.cfg = cfg
        api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else cfg.api_key
        self._client = OpenAI(base_url=cfg.base_url, api_key=api_key or "EMPTY",
                              timeout=cfg.timeout, max_retries=0)
        self._cache = _SqliteCache(cfg.cache_path) if cfg.cache_path else None
        self.stats: Counter = Counter()
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------ public API
    def chat(
        self,
        messages: list[dict],
        *,
        n: int = 1,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tag: str = "chat",
        cache_salt: str = "",
        seed_offset: int = 0,
    ) -> list[str]:
        """Return `n` completions. `tag` labels the call in cost accounting.

        `seed_offset` shifts the request seed so repeated identical prompts (e.g. several
        sampled candidate questions) still get different samples while staying reproducible.
        """
        seed = None if self.cfg.seed is None else self.cfg.seed + seed_offset
        temperature = self.cfg.temperature if temperature is None else temperature
        max_tokens = max_tokens or self.cfg.max_tokens
        extra_body = json.loads(json.dumps(self.cfg.extra_body))
        if self.cfg.disable_thinking:
            extra_body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False

        key = None
        if self._cache is not None:
            key = hashlib.sha256(json.dumps(
                {"m": self.cfg.model, "msgs": messages, "n": n, "t": temperature,
                 "mt": max_tokens, "seed": seed, "eb": extra_body, "salt": cache_salt},
                sort_keys=True).encode()).hexdigest()
            hit = self._cache.get(key)
            if hit is not None:
                self._count(tag, cached=True)
                return hit

        outs: list[str] = []
        attempt_seed = seed
        while len(outs) < n:
            # Some servers (e.g. Ollama) ignore n>1; top up with extra calls.
            got = self._call(messages, n - len(outs), temperature, max_tokens, extra_body, attempt_seed)
            outs.extend(got)
            if attempt_seed is not None:
                attempt_seed += 1
        outs = outs[:n]
        self._count(tag, cached=False)
        if self._cache is not None and key is not None:
            self._cache.put(key, outs)
        return outs

    def complete(self, messages: list[dict], **kw) -> str:
        return self.chat(messages, n=1, **kw)[0]

    # ------------------------------------------------------------------ internals
    def _call(self, messages, n, temperature, max_tokens, extra_body, seed) -> list[str]:
        delay = 2.0
        for attempt in range(self.cfg.max_retries + 1):
            try:
                kwargs: dict[str, Any] = dict(model=self.cfg.model, messages=messages, n=n,
                                              temperature=temperature, max_tokens=max_tokens)
                if seed is not None:
                    kwargs["seed"] = seed
                if extra_body:
                    kwargs["extra_body"] = extra_body
                resp = self._client.chat.completions.create(**kwargs)
                return [strip_thinking(c.message.content or "") for c in resp.choices]
            except Exception as e:  # noqa: BLE001 - surface after retries
                if attempt == self.cfg.max_retries:
                    raise
                log.warning("LLM call failed (%s), retry %d in %.0fs", e, attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise RuntimeError("unreachable")

    def _count(self, tag: str, cached: bool) -> None:
        with self._stats_lock:
            self.stats[tag] += 1
            if cached:
                self.stats[f"{tag}:cached"] += 1


class MeteredLLM:
    """Per-session view of an LLM client that tallies calls into a session-owned Counter.

    Sessions run concurrently, so global counters can't attribute cost to a session;
    the agent wraps the shared client in one of these for each step instead.
    """

    def __init__(self, llm: Any, counter: Counter):
        self._llm = llm
        self._counter = counter

    def chat(self, messages: list[dict], **kw) -> list[str]:
        self._counter[f"llm:{kw.get('tag', 'chat')}"] += 1
        return self._llm.chat(messages, **kw)

    def complete(self, messages: list[dict], **kw) -> str:
        return self.chat(messages, n=1, **kw)[0]
