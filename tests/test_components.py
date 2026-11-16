import json

import pytest
import torch

from agentir.agent.context import candidate_set_size, spherical_kmeans
from agentir.agent.rankers import _parse_answers, jaccard_divergence, js_divergence
from agentir.config import RunConfig, load_config
from agentir.llm import strip_thinking


def test_candidate_set_schedule_matches_plugir():
    assert [candidate_set_size(500, r) for r in range(1, 11)] == list(range(500, 0, -50))


def test_parse_answers():
    assert _parse_answers('Sure: ["red", "blue", "red", "green"]', 3) == ["red", "blue", "green"]
    assert _parse_answers("1. red\n2. blue\n- green", 3) == ["red", "blue", "green"]


def test_divergences():
    same = torch.tensor([[0.9, 0.1, 0.5, 0.2]] * 3)
    diff = torch.eye(4)[:3] + 0.01
    pool = torch.arange(4)
    assert jaccard_divergence(same, 2) == 0.0
    assert jaccard_divergence(diff, 1) == 1.0
    assert js_divergence(same, pool, 0.05) == pytest.approx(0.0, abs=1e-6)
    assert js_divergence(diff, pool, 0.05) > 0.5


def test_kmeans_separates_clusters():
    a = torch.tensor([1.0, 0, 0]) + 0.01 * torch.randn(20, 3, generator=torch.Generator().manual_seed(0))
    b = torch.tensor([0, 1.0, 0]) + 0.01 * torch.randn(20, 3, generator=torch.Generator().manual_seed(1))
    labels = spherical_kmeans(torch.cat([a, b]), 2, seed=0)
    assert len(set(labels[:20].tolist())) == 1 and len(set(labels[20:].tolist())) == 1
    assert labels[0] != labels[20]


def test_strip_thinking():
    assert strip_thinking("<think>hmm</think>\nwhat color?") == "what color?"
    assert strip_thinking("hmm</think> what color?") == "what color?"


def test_config_merge_and_fingerprint(tmp_path):
    a = tmp_path / "a.yaml"
    a.write_text("agent:\n  llm:\n    model: m1\n    base_url: http://x/v1\n")
    b = tmp_path / "b.yaml"
    b.write_text("agent:\n  ranker:\n    kind: answer_divergence\n")
    cfg = load_config([str(a), str(b)], ["eval.max_rounds=5", "agent.llm.base_url=http://y/v1"])
    assert cfg.agent.llm.model == "m1" and cfg.agent.ranker.kind == "answer_divergence"
    assert cfg.eval.max_rounds == 5
    # endpoint location does not change the fingerprint; the model does
    other = load_config([str(a), str(b)], ["eval.max_rounds=5"])
    assert cfg.fingerprint() == other.fingerprint()
    assert cfg.fingerprint() != load_config([str(a), str(b)], ["eval.max_rounds=5", "agent.llm.model=m2"]).fingerprint()


def test_repo_configs_parse():
    import glob

    files = sorted(glob.glob("configs/**/*.yaml", recursive=True))
    assert files
    base = "configs/base.yaml"
    for f in files:
        load_config([base, f] if f != base else [base])


def test_backend_extra_body_replaces_not_merges():
    base = load_config(["configs/base.yaml"])
    assert base.agent.llm.extra_body == {"reasoning_effort": "none"} and not base.agent.llm.disable_thinking
    vllm = load_config(["configs/base.yaml", "configs/backends/vllm.yaml"])
    assert vllm.agent.llm.extra_body == {} and vllm.agent.llm.disable_thinking
    ds = load_config(["configs/base.yaml", "configs/backends/dashscope.yaml"])
    assert ds.agent.llm.extra_body == {"enable_thinking": False}
    # laptop and Colab must be comparable: same fingerprint
    lap = load_config(["configs/base.yaml", "configs/backends/ollama.yaml"])
    colab = load_config(["configs/base.yaml", "configs/backends/colab.yaml"])
    assert lap.fingerprint() == colab.fingerprint() == base.fingerprint()
