"""A tiny synthetic world for tests and `agentir smoke` (no models, no downloads).

Images are attribute tuples (object, color, place, count, time); their "embeddings" are
hashed bag-of-words of the full description, the caption omits some attributes, a
scripted user answers from the hidden attributes, and `MockLLM` imitates each prompt
type well enough for the whole pipeline (generation, filtering, KL/divergence ranking,
reformulation) to run end to end. Numbers from this world mean nothing; it exists to
prove the plumbing works on any machine in a few seconds.
"""
from __future__ import annotations

import itertools
import json
import random
import re
from collections import Counter

import torch

from .retrieval.encoders import FakeEncoder
from .retrieval.index import CorpusIndex

OBJECTS = ["dog", "cat", "car", "bus", "horse", "bike", "boat", "kite"]
COLORS = ["red", "blue", "green", "white", "black", "yellow"]
PLACES = ["park", "street", "beach", "kitchen", "field", "harbor"]
COUNTS = ["one", "two", "three"]
TIMES = ["day", "night"]

QUESTIONS = {
    "color": "what color is the {obj}?",
    "count": "how many {obj}s are there?",
    "time": "is it day or night?",
    "place": "where is the {obj}?",
}
VALUES = {"color": COLORS, "count": COUNTS, "time": TIMES, "place": PLACES}


def describe(a: dict) -> str:
    return f"{a['count']} {a['color']} {a['obj']} in a {a['place']} at {a['time']}"


def build_fake_world(n_images: int = 600, n_queries: int = 40, seed: int = 0):
    rng = random.Random(seed)
    combos = list(itertools.product(OBJECTS, COLORS, PLACES, COUNTS, TIMES))
    rng.shuffle(combos)
    attrs = [dict(zip(["obj", "color", "place", "count", "time"], c)) for c in combos[:n_images]]
    enc = FakeEncoder()
    paths = [f"fake/{i:06d}.jpg" for i in range(n_images)]
    descs = [describe(a) for a in attrs]
    noise = torch.Generator().manual_seed(seed)
    img = enc.encode_text(descs) + 0.05 * torch.randn(n_images, enc.dim, generator=noise)
    img = torch.nn.functional.normalize(img, dim=-1)
    captions = [f"a {a['obj']} in a {a['place']}" for a in attrs]
    index = CorpusIndex(paths=paths, image_emb=img, captions=captions,
                        caption_emb=enc.encode_text(captions))
    targets = rng.sample(range(n_images), n_queries)
    queries = [{"img": paths[i], "dialog": [f"a {attrs[i]['obj']} somewhere"]} for i in targets]
    lookup = dict(zip(paths, attrs))
    return enc, index, queries, lookup


def scripted_answer(lookup: dict):
    def answer(image_path: str, question: str) -> str:
        key = next(p for p in lookup if image_path.endswith(p))
        a = lookup[key]
        q = question.lower()
        if "color" in q:
            return a["color"]
        if "how many" in q:
            return a["count"]
        if "day or night" in q:
            return f"at {a['time']}"
        if "where" in q:
            return f"in a {a['place']}"
        return "i can't tell"
    return answer


class MockLLM:
    """Pattern-matches the prompts in agentir.agent.prompts and answers plausibly."""

    def __init__(self, seed: int = 0):
        self.seed = seed
        self.stats: Counter = Counter()

    def complete(self, messages, **kw) -> str:
        return self.chat(messages, n=1, **kw)[0]

    def chat(self, messages, n: int = 1, tag: str = "chat", seed_offset: int = 0, **_) -> list[str]:
        self.stats[tag] += 1
        sys = messages[0]["content"] if messages[0]["role"] == "system" else ""
        user = messages[-1]["content"]
        if isinstance(user, list):
            user = " ".join(p.get("text", "") for p in user if p.get("type") == "text")
        rng = random.Random(f"{self.seed}:{seed_offset}:{user}")
        if "reconstruct the [Caption]" in sys:
            m = re.search(r"\[Caption\]: (.*?) \[Dialogue\]: (.*?)\s+\[New Caption\]", user, re.S)
            cap, dia = (m.group(1), m.group(2)) if m else (user, "")
            facts = [x.split("?", 1)[1].strip() for x in dia.split(", ") if "?" in x]
            return [", ".join([cap] + [f for f in facts if f and "can't tell" not in f])] * n
        if "paraphrase" in sys:
            return [user.split("Text: ", 1)[-1].split("\nParaphrased", 1)[0] + "."] * n
        if "question generator" in sys:
            obj = next((o for o in OBJECTS if o in user.split("[Description]")[-1]), "object")
            asked = {k for k, t in QUESTIONS.items() if t.format(obj=obj) in user}
            pool = [k for k in QUESTIONS if k not in asked] or list(QUESTIONS)
            return [QUESTIONS[rng.choice(pool)].format(obj=obj) + " Explanation: mock." for _ in range(n)]
        if "according to the given context" in sys:
            ctx = user.split("[Context]\n", 1)[-1].split("\n[Question]", 1)[0]
            q = user.split("[Question]\n", 1)[-1]
            for k, t in QUESTIONS.items():
                if t.split("{")[0] in q and any(v in ctx for v in VALUES[k]):
                    return ["yes"] * n
            return ["Uncertain"] * n
        if "plausible answers" in user or "list the different short answers" in sys:
            q = user.split("[Candidate question]\n", 1)[-1].split("\n", 1)[0]
            known = user.split("[Description]", 1)[-1]
            for k, t in QUESTIONS.items():
                if t.split("{")[0] in q:
                    already = [v for v in VALUES[k] if re.search(rf"\b{v}\b", known)]
                    vals = already * 3 if already else rng.sample(VALUES[k], min(3, len(VALUES[k])))
                    return [json.dumps(vals[:3])] * n
            return [json.dumps(["yes", "no"])] * n
        return ["i can't tell"] * n
