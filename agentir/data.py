"""Evaluation samples: VisDial val dialogs whose images sit inside the 50k search space."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .config import DataConfig


@dataclass
class Sample:
    qid: int  # index into VisDial_v1.0_queries_val.json
    target: str  # relative path inside the corpus, e.g. "unlabeled2017/000000185565.jpg"
    caption: str  # round-0 query
    human_dialog: list[str]  # VisDial human dialog [caption, "q a", ...] (for reference only)

    def image_path(self, corpus_dir: str) -> str:
        return os.path.join(corpus_dir, self.target)


def load_samples(cfg: DataConfig) -> list[Sample]:
    with open(cfg.queries) as f:
        raw = json.load(f)
    ids = list(range(len(raw)))
    if cfg.subset_file:
        with open(cfg.subset_file) as f:
            ids = json.load(f)
    ids = ids[cfg.start:]
    if cfg.limit is not None:
        ids = ids[: cfg.limit]
    return [Sample(i, raw[i]["img"], raw[i]["dialog"][0], raw[i]["dialog"]) for i in ids]
