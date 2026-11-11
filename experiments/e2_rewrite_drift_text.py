"""E2a (text only): does the query rewrite keep what the user said?

Runs on any reconstructed-dialog file (PlugIR's released *_reconed.json, or the
`plugir_reconed.json` an AgentIR run exports). No images, GPU or LLM needed.

For every dialog it tracks content details (noun lemmas, and colour+noun pairs) across
the rewritten captions q_0 .. q_R and reports:
  * retention   - share of the round-0 nouns still present at round r
  * drops       - details present at round r-1 and gone at round r (a rewrite forgot them)
  * flip-flops  - details that disappear and later come back (unstable rewrite)
  * colour conflicts - a noun gets a colour that contradicts an earlier colour for it
  * leaks       - rewrites that copy prompt context into the query ("... among the
                  retrieval candidates"), i.e. other images' captions steering the search
  * length growth (words)

    python experiments/e2_rewrite_drift_text.py dialogues/ours_final_..._reconed.json --out runs/e2a
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import re

import numpy as np

COLORS = {"red", "blue", "green", "yellow", "white", "black", "brown", "gray", "grey", "orange",
          "pink", "purple", "silver", "gold", "tan", "beige"}
# Text that belongs to the prompt or to other images, not to the target: the rewrite copied
# the questioner's context ("... among the retrieval candidates") into the search query.
LEAK = re.compile(r"retrieval candidate|candidates|mentioned|the caption|the dialog|unknown|"
                  r"no information|cannot be determined|not sure", re.I)
SKIP = {"image", "picture", "photo", "photograph", "background", "foreground", "side", "front",
        "top", "bottom", "middle", "object", "item", "thing", "color", "colour", "design", "purpose"}


def load_nlp():
    import spacy

    return spacy.load("en_core_web_sm", disable=["ner"])


def details(doc) -> tuple[set[str], dict[str, set[str]]]:
    nouns, colours = set(), defaultdict(set)
    for t in doc:
        if t.pos_ in ("NOUN", "PROPN") and t.lemma_.lower() not in SKIP and len(t.lemma_) > 2:
            n = t.lemma_.lower()
            nouns.add(n)
            for c in t.children:
                if c.dep_ in ("amod", "compound") and c.lemma_.lower() in COLORS:
                    colours[n].add("gray" if c.lemma_.lower() == "grey" else c.lemma_.lower())
    return nouns, colours


def analyse(dialogs: list[dict], nlp, rounds: int) -> dict:
    texts = [d["dialog"][: rounds + 1] for d in dialogs]
    flat = [t for ts in texts for t in ts]
    docs = list(nlp.pipe(flat, batch_size=256))
    per = []
    i = 0
    for ts in texts:
        ds = docs[i : i + len(ts)]
        i += len(ts)
        per.append([details(d) for d in ds])

    R = rounds
    retention = np.full((len(per), R + 1), np.nan)
    drops = np.zeros((len(per), R))
    flipflop = np.zeros(len(per))
    conflict = np.zeros(len(per))
    words = np.array([[len(t.split()) for t in ts] + [np.nan] * (R + 1 - len(ts)) for ts in texts])
    leak = np.array([[bool(LEAK.search(t)) for t in ts[1:]] + [False] * (R + 1 - len(ts)) for ts in texts])
    examples = []
    for n, rows in enumerate(per):
        base = rows[0][0]
        for r, (nouns, _) in enumerate(rows):
            # soft match: "track" survives as "racetrack", "shirt" as "shirtless"
            low = texts[n][r].lower()
            kept = [b for b in base if b in nouns or b in low or any(b in x or x in b for x in nouns if len(x) > 3)]
            retention[n, r] = len(kept) / len(base) if base else np.nan
        seen_colour: dict[str, set[str]] = defaultdict(set)
        ever, gone = set(), set()
        for r in range(1, len(rows)):
            prev, cur = rows[r - 1][0], rows[r][0]
            lost = prev - cur
            drops[n, r - 1] = len(lost)
            gone |= lost
            back = cur & gone
            if back:
                flipflop[n] = 1
            ever |= cur
        for r, (_, cols) in enumerate(rows):
            for noun, cs in cols.items():
                if seen_colour[noun] and not (cs & seen_colour[noun]):
                    conflict[n] = 1
                    if len(examples) < 15:
                        examples.append({"dialog": n, "img": dialogs[n]["img"], "round": r, "noun": noun,
                                         "earlier": sorted(seen_colour[noun]), "now": sorted(cs)})
                seen_colour[noun] |= cs
    return {
        "num_dialogs": len(per),
        "words_per_round": np.nanmean(words, 0).round(1).tolist(),
        "round0_noun_retention_per_round": np.nanmean(retention, 0).round(3).tolist(),
        "share_dialogs_losing_a_round0_noun_by_last_round": float(np.nanmean(retention[:, -1] < 1)),
        "share_rounds_dropping_a_detail": float((drops > 0).mean()),
        "share_rounds_dropping_a_detail_per_round": (drops > 0).mean(0).round(3).tolist(),
        "share_dialogs_with_flip_flop": float(flipflop.mean()),
        "share_dialogs_with_colour_conflict": float(conflict.mean()),
        "share_rewrites_with_leaked_prompt_text": float(leak.mean()),
        "share_dialogs_with_leaked_prompt_text": float(leak.any(1).mean()),
        "leak_examples": [{"dialog": int(n), "round": int(r) + 1, "text": texts[n][r + 1]}
                          for n, r in zip(*np.nonzero(leak))][:10],
        "colour_conflict_examples": examples,
        # per-dialog arrays so E2b can correlate drift with rank changes
        "_per_dialog": {"retention_last": retention[:, -1].tolist(), "drops": drops.tolist(),
                         "flipflop": flipflop.tolist(), "conflict": conflict.tolist(),
                         "leak": leak.astype(int).tolist()},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reconed", help="reconstructed-dialog JSON: [{img, dialog: [q0, q1, ...]}]")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="runs/e2a_rewrite_drift")
    a = ap.parse_args()
    dialogs = json.load(open(a.reconed))[: a.limit]
    res = analyse(dialogs, load_nlp(), a.rounds)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "e2a_metrics.json").write_text(json.dumps(res, indent=2))
    show = {k: v for k, v in res.items() if not k.startswith("_") and k not in ("colour_conflict_examples", "leak_examples")}
    print(json.dumps(show, indent=2))
    for e in res["leak_examples"][:3]:
        print("leak:", e)
    for e in res["colour_conflict_examples"][:3]:
        print("colour conflict:", e)


if __name__ == "__main__":
    main()
