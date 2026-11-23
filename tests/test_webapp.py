"""The human-in-the-loop web app, driven through its HTTP API on the synthetic world."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from webapp.backends import build_demo_backend  # noqa: E402
from webapp.server import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    log = tmp_path / "contrib.jsonl"
    return TestClient(create_app(build_demo_backend(), log)), log


def test_challenge_session_round_trip(client):
    c, log = client
    ch = c.get("/api/challenge").json()
    s = c.post("/api/sessions", json={"caption": ch["caption"], "target": ch["target"]}).json()
    sid = s["session_id"]
    assert s["results"] and s["target_rank"] >= 1

    q = c.post(f"/api/sessions/{sid}/ask").json()
    assert q["question"] and q["candidates"] and q["criterion"] == "min_kl"
    ans = c.post(f"/api/sessions/{sid}/simulate", json={"question": q["question"]}).json()["answer"]
    s = c.post(f"/api/sessions/{sid}/turn", json={"question": q["question"], "answer": ans}).json()
    assert s["round"] == 2 and len(s["rank_history"]) == 2
    assert ans in s["query"] or s["query"] != ch["caption"]  # the answer reached the retrieval query

    # a question the person wrote themselves is folded in the same way
    s = c.post(f"/api/sessions/{sid}/turn", json={"question": "is it day or night?", "answer": "at night",
                                                  "source": "user"}).json()
    assert "night" in s["query"] and s["turns"][-1]["source"] == "user"

    s = c.post(f"/api/sessions/{sid}/undo").json()
    assert s["round"] == 2 and len(s["rank_history"]) == 2

    c.post(f"/api/sessions/{sid}/finish", json={"found": ch["target"], "rating": 4})
    exported = c.get("/api/contributions.json").json()
    assert exported == [{"img": ch["target"], "dialog": [ch["caption"], f"{q['question']} {ans}"],
                         "session_id": sid, "source": "challenge"}]
    events = [json.loads(ln)["event"] for ln in log.read_text().splitlines()]
    assert events == ["start", "ask", "turn", "turn", "undo", "finish"]
    assert c.post(f"/api/sessions/{sid}/ask").status_code == 409


def test_free_mode_and_images(client):
    c, _ = client
    s = c.post("/api/sessions", json={"caption": "a red dog in a park"}).json()
    assert s["target"] is None and s["target_rank"] is None
    assert c.post(f"/api/sessions/{s['session_id']}/simulate", json={"question": "what color?"}).status_code == 400
    r = c.get("/images/" + s["results"][0]["path"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")
    assert c.get("/").status_code == 200
