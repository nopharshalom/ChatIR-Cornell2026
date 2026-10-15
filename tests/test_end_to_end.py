"""Full loop on the synthetic world, in-process and through the two HTTP services."""
import json

import pytest

from agentir.agent import Agent
from agentir.config import RunConfig
from agentir.data import Sample
from agentir.orchestrator import (HttpAgentHandle, HttpUserHandle, LocalAgentHandle, LocalUserHandle,
                                  run_dir, run_eval)
from agentir.retrieval import Retriever, SearchTool
from agentir.smoke import MockLLM, build_fake_world, scripted_answer
from agentir.user_sim import ScriptedUserSimulator


@pytest.fixture(scope="module")
def world():
    enc, index, queries, lookup = build_fake_world(n_images=300, n_queries=8)
    retriever = Retriever(enc, index)
    samples = [Sample(i, q["img"], q["dialog"][0], q["dialog"]) for i, q in enumerate(queries)]
    return retriever, samples, lookup


def _cfg(tmp_path, ranker):
    return RunConfig.model_validate({
        "name": f"t-{ranker}", "output_dir": str(tmp_path), "retriever": {"backend": "fake"},
        "agent": {"generator": {"thres_low": 100, "n_clusters": 4, "num_candidates": 3},
                  "ranker": {"kind": ranker, "top_k": 10, "thres_low": 100, "pool_size": 100}},
        "eval": {"max_rounds": 3, "ks": [1, 10], "workers": 2}})


@pytest.mark.parametrize("ranker", ["first", "random", "plugir_kl", "answer_divergence"])
def test_local_loop(tmp_path, world, ranker):
    retriever, samples, lookup = world
    cfg = _cfg(tmp_path, ranker)
    agent = Agent(cfg.agent, MockLLM(), SearchTool(retriever))
    user = ScriptedUserSimulator(scripted_answer(lookup))
    m = run_eval(cfg, samples, LocalAgentHandle(agent), LocalUserHandle(user, "."), retriever)
    assert m["num_sessions"] == len(samples) and m["num_rounds"] == 3
    recs = [json.loads(x) for x in open(run_dir(cfg) / "sessions.jsonl")]
    assert all(len(r["ranks"]) == 4 and len(r["rounds"]) == 3 for r in recs)
    assert m["hits"][10][-1] >= m["hits"][10][0]
    assert (run_dir(cfg) / "plugir_reconed.json").exists()
    if ranker == "answer_divergence":
        assert m["cost_per_round"].get("search:probe", 0) > 0


def test_resume_skips_done_sessions(tmp_path, world):
    retriever, samples, lookup = world
    cfg = _cfg(tmp_path, "first")
    llm = MockLLM()
    agent = Agent(cfg.agent, llm, SearchTool(retriever))
    user = LocalUserHandle(ScriptedUserSimulator(scripted_answer(lookup)), ".")
    run_eval(cfg, samples[:4], LocalAgentHandle(agent), user, retriever)
    calls = sum(llm.stats.values())
    run_eval(cfg, samples[:4], LocalAgentHandle(agent), user, retriever)
    assert sum(llm.stats.values()) == calls


def test_http_services(tmp_path, world):
    from fastapi.testclient import TestClient

    from agentir.services.agent_app import create_app as agent_app
    from agentir.services.user_app import create_app as user_app

    retriever, samples, lookup = world
    cfg = _cfg(tmp_path, "answer_divergence")
    cfg.eval.workers = 1
    corpus = tmp_path / "corpus"
    for s in samples:  # user service checks the target file exists under corpus_dir
        (corpus / s.target).parent.mkdir(parents=True, exist_ok=True)
        (corpus / s.target).write_bytes(b"")
    cfg.data.corpus_dir = str(corpus)
    agent = Agent(cfg.agent, MockLLM(), SearchTool(retriever))
    a = HttpAgentHandle("http://unused")
    a.c = TestClient(agent_app(cfg, agent=agent))
    u = HttpUserHandle("http://unused")
    u.c = TestClient(user_app(cfg, user_sim=ScriptedUserSimulator(scripted_answer(lookup))))
    assert a.c.get("/health").json()["ok"]
    m = run_eval(cfg, samples[:3], a, u, retriever)
    assert m["num_sessions"] == 3
    bad = u.c.post("/answer", json={"target": "../../etc/passwd", "question": "q"})
    assert bad.status_code == 400
