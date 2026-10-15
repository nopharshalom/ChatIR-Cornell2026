"""Assemble the ChatIR/PlugIR 50k search space on disk.

The protocol JSONs are already in this repo (Protocol/, dialogues/). This builds the image
side, in the same layout as PlugIR's eval.py --data-dir and the Colab notebook:

    data/unlabeled2017/<12-digit id>.jpg     (the 50,000 images of Search_Space_val_50k.json)

Every protocol path is named `unlabeled2017/<id>.jpg`, and PlugIR's README (and PlugIR_Colab.ipynb in
this repo) build the corpus from the COCO unlabeled2017 zip alone,
which implies the 2,064 VisDial val targets are themselves COCO-unlabeled images. So:

1. All 50k paths are taken from COCO unlabeled2017 (local zip, or fetched one by one).
2. Only if some *targets* are still missing, they are filled from the VisDial v1.0 val image
   zip (VisualDialog_val2018_<id>.jpg -> unlabeled2017/<id>.jpg). Confirm with Matan.

Every protocol path is checked at the end and the result written to MANIFEST.json.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

VISDIAL_VAL_IMAGES = "https://www.dropbox.com/s/twmtutniktom7tu/VisualDialog_val2018.zip?dl=1"
COCO_UNLABELED_ZIP = "http://images.cocodataset.org/zips/unlabeled2017.zip"  # ~19 GB, 123k images
COCO_IMAGE_URL = "http://images.cocodataset.org/unlabeled2017/{name}"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url} -> {dest}")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        with tqdm(total=total, unit="B", unit_scale=True) as bar:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                bar.update(len(chunk))
    tmp.rename(dest)


def place_targets(visdial_zip: Path, out_dir: Path, targets: set[str]) -> int:
    """Extract VisualDialog_val2018_<id>.jpg as unlabeled2017/<id>.jpg for every target."""
    want = {Path(t).name: t for t in targets}
    n = 0
    with zipfile.ZipFile(visdial_zip) as z:
        for info in z.infolist():
            base = Path(info.filename).name
            if not base.startswith("VisualDialog_val2018_"):
                continue
            rel = want.get(base.replace("VisualDialog_val2018_", ""))
            if rel is None:
                continue
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                with z.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            n += 1
    return n


def place_from_coco_zip(coco_zip: Path, out_dir: Path, needed: set[str]) -> int:
    n = 0
    with zipfile.ZipFile(coco_zip) as z:
        for info in tqdm(z.infolist(), desc="scanning COCO zip"):
            rel = "unlabeled2017/" + Path(info.filename).name
            if rel in needed and not (out_dir / rel).exists():
                (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out_dir / rel, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                n += 1
    return n


def place_from_coco_urls(out_dir: Path, needed: set[str], workers: int = 32) -> int:
    todo = [r for r in needed if not (out_dir / r).exists()]

    def get(rel: str) -> bool:
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(4):
            try:
                urllib.request.urlretrieve(COCO_IMAGE_URL.format(name=Path(rel).name), dest)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    ok = 0
    with ThreadPoolExecutor(workers) as ex:
        for f in tqdm(as_completed([ex.submit(get, r) for r in todo]), total=len(todo), desc="COCO images"):
            ok += f.result()
    return ok


def prepare(data_dir: str = "data", search_space: str = "Protocol/Search_Space_val_50k.json",
            queries: str = "dialogues/VisDial_v1.0_queries_val.json", visdial_zip: str | None = None,
            coco_zip: str | None = None, coco_by_url: bool = False) -> None:
    corpus = Path(data_dir)
    space = json.load(open(search_space))
    targets = {q["img"] for q in json.load(open(queries))}

    needed = set(space)
    if coco_zip:
        print(f"images from COCO zip: {place_from_coco_zip(Path(coco_zip), corpus, needed)}")
    elif coco_by_url:
        print(f"images downloaded from COCO: {place_from_coco_urls(corpus, needed)}")
    else:
        print("no COCO source given: pass --coco-zip PATH or --coco-by-url")

    missing_targets = {t for t in targets if not (corpus / t).exists()}
    if missing_targets:
        print(f"{len(missing_targets)} targets not in COCO unlabeled2017 -> filling from VisDial val zip")
        vz = Path(visdial_zip) if visdial_zip else corpus / "downloads" / "VisualDialog_val2018.zip"
        if not vz.exists():
            _download(VISDIAL_VAL_IMAGES, vz)
        print(f"targets placed from VisDial: {place_targets(vz, corpus, missing_targets)}")

    missing = [p for p in space if not (corpus / p).exists()]
    status = {"expected": len(space), "present": len(space) - len(missing), "missing_examples": missing[:10]}
    (corpus / "MANIFEST.json").write_text(json.dumps(status, indent=2))
    print(json.dumps(status, indent=2))
    if missing:
        print("corpus incomplete - rerun with the missing sources before `agentir build-index`")
