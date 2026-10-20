import json
import zipfile

import torch

from agentir.prepare_data import place_targets
from agentir.retrieval.index import CorpusIndex, load_captions


def test_targets_renamed_into_protocol_layout(tmp_path):
    z = tmp_path / "VisualDialog_val2018.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("VisualDialog_val2018/VisualDialog_val2018_000000185565.jpg", b"img")
        f.writestr("VisualDialog_val2018/VisualDialog_val2018_000000000001.jpg", b"other")
    n = place_targets(z, tmp_path / "corpus", {"unlabeled2017/000000185565.jpg"})
    assert n == 1
    assert (tmp_path / "corpus/unlabeled2017/000000185565.jpg").read_bytes() == b"img"
    assert not (tmp_path / "corpus/unlabeled2017/000000000001.jpg").exists()


def test_index_roundtrip_and_captions(tmp_path):
    caps = tmp_path / "caps.json"
    caps.write_text(json.dumps([{"id": "b.jpg", "caption": ["a dog"]}, {"id": "a.jpg", "caption": ["a cat"]}]))
    assert load_captions(str(caps), ["a.jpg", "b.jpg", "c.jpg"]) == ["a cat", "a dog", ""]
    idx = CorpusIndex(["a.jpg", "b.jpg"], torch.nn.functional.normalize(torch.randn(2, 8), dim=-1),
                      ["a cat", "a dog"], torch.randn(2, 8))
    idx.save(tmp_path / "i.pt")
    back = CorpusIndex.load(tmp_path / "i.pt")
    assert back.paths == idx.paths and back.index_of("b.jpg") == 1
    assert torch.allclose(back.image_emb, idx.image_emb, atol=1e-2)


def test_reuse_plugir_eval_cache(tmp_path):
    """eval.py saves (ids, vectors) in DataLoader order; we must realign by id."""
    from agentir.retrieval.encoders import FakeEncoder
    from agentir.retrieval.index import with_caption_embeddings

    space = ["a.jpg", "b.jpg", "c.jpg"]
    vecs = torch.nn.functional.normalize(torch.randn(3, 512), dim=-1)
    perm = torch.tensor([2, 0, 1])
    torch.save((perm, vecs[perm]), tmp_path / "corpus_blip.pth")
    idx = CorpusIndex.from_plugir_cache(str(tmp_path / "corpus_blip.pth"), space)
    assert torch.allclose(idx.image_emb, vecs, atol=1e-6)
    idx = with_caption_embeddings(idx, FakeEncoder(), ["a cat", "a dog", ""])
    assert idx.caption_emb.shape == (3, 512) and idx.captions[1] == "a dog"


def test_default_paths_point_at_repo_protocol_files():
    import os

    from agentir.config import DataConfig

    d = DataConfig()
    for p in (d.search_space, d.captions, d.queries):
        assert os.path.exists(p), p
