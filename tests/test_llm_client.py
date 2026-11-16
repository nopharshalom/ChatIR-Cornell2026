"""LLMClient against a fake OpenAI-compatible server (no network, no model)."""
import socket
import threading
import time

import pytest

from agentir.config import LLMConfig
from agentir.llm import LLMClient


@pytest.fixture(scope="module")
def fake_server():
    import uvicorn
    from fastapi import FastAPI, Request

    app = FastAPI()
    seen: list[dict] = []

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        seen.append(body)
        text = f"<think>pondering</think>reply seed={body.get('seed')}"
        return {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                # behaves like Ollama: always one choice, whatever n was requested
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}]}

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1", seen
    server.should_exit = True


def test_client_tops_up_n_strips_thinking_and_caches(tmp_path, fake_server):
    url, seen = fake_server
    llm = LLMClient(LLMConfig(base_url=url, model="m", seed=7, cache_path=str(tmp_path / "c.sqlite")))
    outs = llm.chat([{"role": "user", "content": "hi"}], n=3, tag="generate")
    assert outs == ["reply seed=7", "reply seed=8", "reply seed=9"]  # distinct seeds per top-up
    assert seen[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    n_requests = len(seen)
    again = llm.chat([{"role": "user", "content": "hi"}], n=3, tag="generate")
    assert again == outs and len(seen) == n_requests  # served from cache
    assert llm.stats["generate:cached"] == 1


def test_image_message(tmp_path, fake_server):
    from PIL import Image

    from agentir.llm import user_message_with_image

    url, seen = fake_server
    p = tmp_path / "x.jpg"
    Image.new("RGB", (1600, 900), "red").save(p)
    msg = user_message_with_image("what is it?", p, max_side=256)
    assert msg["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    llm = LLMClient(LLMConfig(base_url=url, model="m", cache_path=None))
    assert llm.complete([msg]).startswith("reply")


def test_team_default_sends_ollama_thinking_switch(tmp_path, fake_server):
    from agentir.config import load_config

    url, seen = fake_server
    cfg = load_config(["configs/base.yaml"], [f"agent.llm.base_url={url}", "agent.llm.cache_path=null"])
    LLMClient(cfg.agent.llm).complete([{"role": "user", "content": "hi"}])
    body = seen[-1]
    assert body["model"] == "qwen3.5:9b"
    assert body["reasoning_effort"] == "none"
    assert "chat_template_kwargs" not in body
