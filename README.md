# ChatIR-Cornell2026: AgentIR + PlugIR

This repo has two parts:

* **`agentir/`**: our system (below). Modular agent, simulated user, retrieval, metrics.
* **The PlugIR reference implementation** (`generate_dialog.py`, `eval.py`, `PlugIR_Colab.ipynb`): the original code, ported to Qwen and Colab. It stays as-is so AgentIR's PlugIR port can be checked against it (see [the bottom of this file](#plugir-reference-implementation)).

## AgentIR

Interactive text-to-image retrieval with an agent that asks the user questions and can
probe the corpus with its own searches. Built on the ChatIR / PlugIR protocol
(VisDial val dialogs, 50k-image search space, frozen BLIP retriever) so results are
directly comparable to both papers.

```
                  ┌──────────── agent service ─────────────┐
 caption ───────▶ │ generator ─▶ ranker ─▶ question        │──── question ───▶ ┌──── user-sim service ────┐
                  │     ▲          ▲                        │                   │ VLM (Qwen3.5) looking at │
                  │  SearchTool (metered probes over 50k)   │ ◀─── answer ───── │ the target image         │
                  │     ▼                                   │                   └──────────────────────────┘
                  │ reformulator ─▶ new retrieval query     │
                  └─────────────────────────────────────────┘
 evaluator: ranks the target under every round's query ─▶ Recall@K, Hits@K, BRI, cost per round
```

The agent never sees the target image or its rank; only the user simulator sees the
image. The orchestrator drives up to 10 rounds of ping-pong, in-process for laptop
development or across the two HTTP services for GPU runs.

---

### 1. Install and check (5 minutes, no downloads)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q            # 21 tests, incl. metric parity with PlugIR's eval.py
agentir smoke        # full loop on a synthetic world with a mock LLM
```

`agentir smoke` exercises every component (PlugIR generator, filter, KL ranker,
answer-divergence ranker, reformulation, resume, export). Its numbers are meaningless;
it only proves the plumbing works on your machine.

### 2. Data: the 50k search space

The protocol files are already in the repo (`Protocol/`, `dialogues/`). The images go in
`data/unlabeled2017/`, the same place `eval.py --data-dir data` and the Colab notebook use:

```bash
agentir prepare-data --coco-zip ~/Downloads/unlabeled2017.zip    # 19 GB zip, if you have it
agentir prepare-data --coco-by-url                               # or fetch only the 50k needed images
```

It checks every protocol path, writes `data/MANIFEST.json`, and fills any VisDial targets
missing from COCO from the VisDial val image zip.

Then embed the corpus once (BLIP ViT-L; roughly 10 min on a GPU, longer on a laptop):

```bash
agentir build-index -c configs/base.yaml -c configs/backends/ollama.yaml   # or backends/vllm.yaml
# already ran eval.py on Colab? reuse its embeddings and only encode the captions:
agentir build-index -c configs/base.yaml --from-plugir-cache cache/corpus_blip.pth
```

Share `cache/index_blip.pt` with the team instead of everyone rebuilding it.

**On Colab:** open [`AgentIR_Colab.ipynb`](AgentIR_Colab.ipynb); it does all of the above plus
the dev-subset baseline runs, using Qwen through DashScope.

### 3. Serve the model

Qwen3.5 is natively vision-language, so one server plays both the questioner and the
simulated user.

| Machine | Command | Config |
|---|---|---|
| GPU box | `vllm serve Qwen/Qwen3.5-9B --port 8000 --max-model-len 32768 --reasoning-parser qwen3` | `configs/backends/vllm.yaml` |
| MacBook | `ollama pull qwen3.5:9b` (or `:4b` on 16 GB) | `configs/backends/ollama.yaml` |
| Qwen API (DashScope, as used in the team's ChatIR-Cornell2026 fork) | `export DASHSCOPE_API_KEY=...` | `configs/backends/dashscope.yaml` |
| Other paid API (if budget is approved) | — | `configs/backends/openai_api.yaml` |

```bash
agentir check-llm -c configs/base.yaml -c configs/backends/ollama.yaml --image data/unlabeled2017/000000185565.jpg
```

This prints latency, warns if reasoning text is leaking into answers, and confirms image
input works. Run it before any long job.

### 4. Milestone 2 workflow

**Step 1: verify the retriever and metrics without any LLM.** Score PlugIR's released
dialogs and the human VisDial dialogs with our BLIP index:

```bash
B="-c configs/base.yaml -c configs/backends/ollama.yaml"
agentir eval-dialogs $B dialogues/ours_final_q_n_5_recall_hitting_10_thres_low_500_recon_true_referring_true_filtering_true_select_true_reconed.json --split   # expect BRI ≈ 0.767 (PlugIR Table 1)
agentir eval-dialogs $B dialogues/VisDial_v1.0_queries_val.json # human dialogs, ChatIR-style concat
```

If the first number is not close to 0.7674, the corpus or index is wrong; stop and fix
that before running anything else.

**Step 2: baselines on the dev subset (first 50 dialogs), then the full 2,064.**

```bash
agentir run $B -c configs/experiments/plugir.yaml -c configs/dev.yaml
agentir run $B -c configs/experiments/random_ranker.yaml -c configs/dev.yaml   # control
agentir run $B -c configs/experiments/chatir.yaml -c configs/dev.yaml
```

**Step 3: our ranker, everything else identical to PlugIR.**

```bash
agentir run $B -c configs/experiments/divergence.yaml -c configs/dev.yaml
agentir run $B -c configs/experiments/divergence.yaml -c configs/dev.yaml --set agent.ranker.divergence=jaccard
```

Each run writes `runs/<name>-<fingerprint>/`:

| File | Contents |
|---|---|
| `metrics.txt` / `metrics.json` | Recall@{1,10,50} and Hits@{1,10,50} per round, BRI, median rank, LLM calls and probe searches per round |
| `sessions.jsonl` | every round: question, answer, new query, all candidates and their scores, the answer paths the divergence ranker simulated, the context captions, timing |
| `plugir_dialogs.json`, `plugir_reconed.json` | same dialogs in PlugIR's format, so their `eval.py --split` can cross-check us |
| `config.yaml`, `fingerprint.txt` | the exact resolved settings |

Runs resume: rerun the same command after a crash and finished sessions are skipped.
Every LLM call is cached in `cache/llm_cache.sqlite`, so rerunning identical settings
costs nothing. Recompute metrics with `agentir metrics runs/<dir>`.

### 5. Two-service mode (agent and user simulator as separate processes)

```bash
agentir serve-user  $B -c configs/experiments/plugir.yaml --port 7002 &
agentir serve-agent $B -c configs/experiments/plugir.yaml --port 7001 &
agentir run $B -c configs/experiments/plugir.yaml --set mode=http
```

Or `scripts/run_services.sh configs/backends/vllm.yaml configs/experiments/plugir.yaml`.
The services can live on different machines; set `agent_url` / `user_url`.

### 6. Where new ideas plug in

| Idea | File | Interface |
|---|---|---|
| Better question **ranking** (core contribution) | `agentir/agent/rankers.py` | `select(state, candidates, llm, search) -> QuestionDecision` |
| Better question **generation** | `agentir/agent/generators.py` | `generate(state, llm, search) -> list[str]` |
| Query reformulation | `agentir/agent/reformulators.py` | `reformulate(state, llm) -> str` |
| Autonomous probing (project description §3) | use `search.similarity([...], purpose="probe")` anywhere in the agent | every call is counted in `cost_per_round` |
| Different simulated user | `agentir/user_sim.py` | `answer(image_path, question, context) -> str` |

Register a new class in the matching `build_*` function and add its knobs to
`agentir/config.py`. `AnswerDivergenceRanker` is a complete worked example: it proposes
answers with the LLM, issues one probe search per answer path, and scores the spread.

### 7. Team rules for comparable numbers

1. Always load `configs/base.yaml` first. It pins the retriever, model, temperatures,
   seed, rounds and K values. Change it only by team agreement.
2. Compare runs only when their fingerprints match except for the setting under study.
   The fingerprint ignores machine-specific settings (URLs, workers, paths) but changes
   with the model tag, so laptop (`qwen3.5:9b` GGUF) and GPU (`Qwen/Qwen3.5-9B`) runs are
   kept apart on purpose.
3. Paper tables come from the full 2,064 dialogs on one backend. `configs/dev.yaml` is
   for iteration only.

### 8. Findings from porting PlugIR (worth raising with Matan)

* **Selection is argmin KL, without answers.** PlugIR embeds `query + ", " + question`
  and keeps the question that moves the candidate distribution *least* (paper Alg. 1
  line 10, `select_question`). No answer is simulated, which is the gap our ranker targets.
* **Their KL is computed on near-uniform distributions.** The softmax is over raw cosine
  similarities (temperature 1) of ~450 candidates, so `p_prev` and `p_q` are both almost
  flat. Our JSD scorer exposes the temperature (`jsd_temperature`, default 0.05).
* **Top-K Jaccard saturates.** In the smoke world, the literal "messiest result set"
  score hit 1.0 for most questions because every answer path reshuffled the top-K
  completely, so it could not break ties. The default is therefore `divergence: jsd`
  (= expected information gain under a uniform answer prior); `jaccard` stays available.
  Check whether this also happens on the real 50k corpus.
* **Generator quirks kept for faithfulness** (`generators.py`, marked `QUIRK`): the
  rejected-question list is never reset between candidates, the fallback triggers only
  when it has exactly 3 items, and the plain (non-referring) branch in their script
  crashes for `q_n > 1`. Candidates are frequently duplicates; our ranker deduplicates.
* **k-means:** PlugIR uses `fast_pytorch_kmeans` (cosine); we use an equivalent seeded
  spherical k-means to drop the dependency. Expect tiny differences in context captions.
* **Corpus assumption:** like PlugIR's README and the team's Colab notebook, all 50k
  images (targets included) are taken from COCO unlabeled2017; the VisDial val zip is
  only a fallback if some targets are missing. Confirm with Matan (ChatIR's first
  author) how the 50k set was built.
* **Simulated user:** PlugIR's BLIP-2 sees the current query plus the question. Our VLM
  default sees only the image and the question (`user_sim.include_caption: false`).
  Pick one as a team; `configs/experiments/plugir_blip2_user.yaml` reproduces theirs exactly.

### Repo map

```
agentir/
  cli.py              agentir <command>
  config.py           typed config + fingerprint
  llm.py              OpenAI-compatible client: retries, n top-up, <think> stripping, SQLite cache
  retrieval/          BLIP / CLIP encoders, cached 50k index, Retriever + metered SearchTool
  agent/              state, prompts (PlugIR's verbatim), context extraction, generators, rankers, reformulators
  user_sim.py         VLM / BLIP-2 / scripted users
  orchestrator.py     ping-pong loop, local + HTTP handles, resume, export
  services/           FastAPI agent and user-sim apps
  metrics.py          Recall@K, Hits@K, BRI (parity-tested against PlugIR)
  prepare_data.py     50k corpus assembly + verification
  smoke.py            synthetic world + mock LLM
configs/              base, backends/, experiments/, dev
tests/
AgentIR_Colab.ipynb   AgentIR on Colab (DashScope Qwen)

# PlugIR reference implementation (unchanged)
generate_dialog.py, eval.py, finetune/, PlugIR_Colab.ipynb
Protocol/, dialogues/ protocol files shared by both
```

---

## PlugIR reference implementation

> **About this copy:** a working copy of [Saehyung-Lee/PlugIR](https://github.com/Saehyung-Lee/PlugIR) (MIT license), modified to:
> - use **Qwen** (OpenAI-compatible API) instead of OpenAI GPT-3.5 for dialogue generation,
> - fix the hardcoded `./ChatIR/...` paths in `generate_dialog.py`,
> - run on **Google Colab**: open [`PlugIR_Colab.ipynb`](PlugIR_Colab.ipynb) in Colab and run the cells top to bottom.
>
> Qwen setup: set `DASHSCOPE_API_KEY` (and `QWEN_BASE_URL` if your key isn't from the international/Singapore region). Choose the model with `--llm_model` (default `qwen-plus`). Python packages: use `openai>=1.0`, not the old `openai` version.
>
> The original README follows.


### [ACL 2024 Oral] Interactive Text-to-Image Retrieval with Large Language Models: A Plug-and-Play Approach

This repository is the official implementation of [Interactive Text-to-Image Retrieval with Large Language Models: A Plug-and-Play Approach](https://arxiv.org/abs/2406.03411), published as a main (oral) paper at ACL 2024.
Our code is based on
1. [ChatIR (Levy et al., NeurIPS 2023)](https://github.com/levymsn/ChatIR)
2. [PyTorch image classification reference](https://github.com/pytorch/vision/tree/main/references/classification)
3. [CLIP](https://github.com/openai/CLIP)

#### Prerequisites
##### Packages
* [pytorch](https://pytorch.org/get-started/locally/)
* [clip](https://github.com/openai/CLIP)
* [transformers](https://github.com/huggingface/transformers)
* [openai](https://openai.com/index/openai-api/)
##### datasets
* [COCO 2017 Unlabeled images](http://images.cocodataset.org/zips/unlabeled2017.zip)
* [VisDial](https://visualdialog.org/data)
```
    VisDial  
    ├── train                    
    │    ├── images          
    │    └── visdial_1.0_train.json                
    └── val
         ├── images  
	 └── visdial_1.0_val.json
```
#### Context Reformulation and Context-aware Dialogue Generation
Our method, PlugIR, actively utilizes the general instruction-following capability of LLMs in two ways. First, by transforming the dialogue-form context into a caption-style query, we eliminate the need to fine-tune a retrieval model on existing visual dialogue data, thereby enabling the use of any arbitrary black-box model. Second, we construct the LLM questioner to generate non-redundant questions about the attributes of the target image, based on the information of retrieval candidate images in the current context. Extracting the textual information about each cluster of retreival candidate for the LLM questioner and filtering the redundant questions generated by LLM questioner, this approach mitigates the issues of noisiness and redundancy in the generated questions.
```PlugIR
python generate_dialog.py --api_key ${YOUR_OPENAI_API_KEY} --q_n 5 --thres_low 500 --n_clusters 10 --reconstruct --referring --filtering --select
```

#### Evaluation
In addition to existing metrics (Hits@K and Recall@K), we introduce the Best log Rank Integral (BRI) metric. BRI is a novel metric aligned with human judgment, specifically designed to provide a comprehensive and quantifiable evaluation of interactive retrieval systems.
##### Zero-shot baseline
```zero-shot
model=blip
python eval.py --retriever ${model} --cache-corpus cache/corpus_${model}.pth --data-dir <a directory path containing "unlabeled2017">
```
##### Fine-tuning baseline
```fine-tuned
model=blip
python eval.py --retriever ${model} --cache-corpus cache/corpus_finetuned_${model}.pth --data-dir <a directory path containing "unlabeled2017"> --ft-model-path <fine-tuned-model.pth>
```
##### PlugIR (ours)
```PlugIR
model=blip
python eval.py --retriever ${model} --cache-corpus cache/corpus_finetuned_${model}.pth --data-dir <a directory path containing "unlabeled2017"> --queries-path <our_queries.json> --split
```

#### BLIP Text Encoder Fine-tuning
```FT
cd finetune
torchrun --rdzv_backend=c10d --rdzv_endpoint=localhost:0 --nnodes=1 --nproc_per_node=4 train.py \
	--data-path <path to the VisDial directory> \
	--amp
```
