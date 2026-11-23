# ChatIR web app: a person in the loop

A user-facing demo of interactive text-to-image retrieval. The person replaces the
simulated user: they describe an image, the agent asks questions, and every answer is
folded into the next retrieval query by the agent's reformulator, exactly as in
`agentir run`. Out of the box the agent is the **PlugIR baseline**
(`configs/experiments/plugir.yaml`).

## Run it

From the repo root, with `pip install -e ".[serve]"` done:

```bash
# PlugIR baseline over the real 50k ChatIR corpus, with the team's Qwen3.5-9B via Ollama
python -m webapp.server -c configs/base.yaml -c configs/backends/ollama.yaml -c configs/experiments/plugir.yaml

# Same, on a laptop without a GPU: smaller questioner (ollama pull gemma3:4b), ~1 min per question
python -m webapp.server -c configs/base.yaml -c configs/backends/ollama_gemma3.yaml -c configs/experiments/plugir.yaml

# Plumbing check only: agentir's synthetic smoke world + mock LLM (made-up images, no models)
python -m webapp.server --demo
```

Open http://127.0.0.1:8000.

Real mode needs `cache/index_blip.pt` (see the main README, step 2; a teammate's copy works)
and a running LLM server (step 3). If you already have PlugIR-format image embeddings from
Colab (`torch.save((ids, embs), ...)`, e.g. `corpus_blip_large.pth`), convert them instead
of re-embedding the images; only the 50k captions get encoded, ~15 min on a CPU:

```bash
agentir build-index -c configs/base.yaml --from-plugir-cache cache/corpus_blip_large.pth
```
 The images do **not** need to be downloaded: if
`data/unlabeled2017/<id>.jpg` is missing locally, the app loads it from
`images.cocodataset.org`. The "let the AI answer" button (VLM user simulator) does need
the target image on disk.

## Share it from Colab (public link, free GPU)

Open [`webapp/ChatIR_WebApp_Colab.ipynb`](https://colab.research.google.com/github/nopharshalom/ChatIR-Cornell2026/blob/feat/interactive-webapp/webapp/ChatIR_WebApp_Colab.ipynb)
in Colab, pick a T4 GPU, and *Run all*. It builds the index from your `corpus_blip_large.pth`
(kept on Google Drive), runs `qwen3.5:9b` with Ollama on the GPU, starts the app, and prints a
`https://….trycloudflare.com` link to share. The link lasts as long as the Colab session; each
session gets a new one. Contributions are written to Drive.

## What a person can do

| Action | What happens in the backend |
|---|---|
| Describe the image | `agent.start(caption)`; the description is the round-0 query |
| Answer the agent's question | `agent.observe(question, answer)`, which reformulates the query and re-searches |
| Pick another question the agent generated | same, logged with `source: "alternative"` |
| Write their own question | same, logged with `source: "user"` |
| Undo | restores the dialog state from before the last answer |
| Click a result | finishes the session (found image, a 1–5 rating, a note) |

**Two modes.** *Find my image*: free search, no ground truth. *Challenge*: the app shows a
random VisDial val target (the agent never sees it). The person describes it and answers,
and the target's rank is shown live after every round, using the same ranking as the
offline evaluator. In challenge mode the VLM user simulator can also answer a question, so
you can compare human and simulated answers on the same target.

The side panels show the PlugIR internals: the candidate questions with their KL scores,
the cluster representatives the questioner was shown, and the full query history.

## Contributions

Every event (start, ask, turn, undo, finish) is appended to
`runs/webapp/contributions.jsonl` (change with `--log`). Each turn records who wrote the
question (`agent` / `alternative` / `user`), who answered (`human` / `simulator`), the
query before and after, and the target rank. `GET /api/contributions.json` exports
finished dialogs in ChatIR/PlugIR format (`{"img", "dialog"}`), so human dialogs can be
scored like any other:

```bash
curl -s localhost:8000/api/contributions.json > dialogues/webapp_human.json
agentir eval-dialogs -c configs/base.yaml dialogues/webapp_human.json
```

## Swapping in a better backend

The app only uses the `InteractiveBackend` interface in `webapp/backends.py`.

* **Another agentir method** is just a config:
  `python -m webapp.server -c configs/base.yaml -c configs/backends/ollama.yaml -c configs/experiments/divergence.yaml`
  (or `--set agent.ranker.kind=answer_divergence`). The header shows the method and the run
  fingerprint, so a demo can be tied to the numbers in a results table.
* **Something that is not an `agentir.Agent`**: implement the methods of
  `InteractiveBackend` (`start`, `ask`, `observe`, `results`, …) and pass your object to
  `webapp.server.create_app(backend)`.

## API

| Method | Path | Body |
|---|---|---|
| GET | `/api/info` | |
| GET | `/api/challenge` | |
| POST | `/api/sessions` | `{caption, target?, k?, participant?}` |
| POST | `/api/sessions/{id}/ask` | |
| POST | `/api/sessions/{id}/turn` | `{question, answer, source, answered_by}` |
| POST | `/api/sessions/{id}/simulate` | `{question}` |
| POST | `/api/sessions/{id}/undo` | |
| POST | `/api/sessions/{id}/finish` | `{found?, rating?, note?}` |
| GET | `/api/contributions.json` | |
| GET | `/images/{path}` | |

Sessions live in memory: restarting the server ends open sessions (the log keeps them).
The server is meant for local demos and user studies on a trusted network; it has no
authentication.
