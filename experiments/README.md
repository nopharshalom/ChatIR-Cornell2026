# Experiments for the Matan meeting

Matan asked for an initial experiment behind each suggestion in the PlugIR / A2R inspection
deck, so we can discuss numbers rather than ideas. There is one script per suggestion, cheapest
first. `Experiments_Colab.ipynb` runs all of them in order on a free T4.

Every script also accepts `--fake`. That runs it on agentir's synthetic world, with no models,
to check the plumbing. The numbers it prints mean nothing.

## 01. Are we showing the questioner the right images?

**E1: `e1_candidate_evidence.py`** (no LLM; about 15 min for 500 dialogs). The script replays
PlugIR's released queries. Before each question, it rebuilds the 10 images (or captions) that each
method would show the questioner:

- PlugIR's lowest-entropy representatives
- the best-ranked image of each of the same clusters
- the top 10
- A2R's DPP-10
- 10 random images (control)

For each set it measures whether the target is shown, how close the closest shown image is to the
target, whether the set stays on-query, and how diverse it is. It also reports how often the target
is outside PlugIR's shrinking pool (500→50) or A2R's fixed top 50, and how often a saved caption
fails to pick out its own image within the pool.

- *Counts as evidence:* rank-aware or DPP sets come much closer to the target than PlugIR's
  representatives, and/or the target often falls outside the pool in late rounds.

**E1b: `configs/experiments/e1_rep_rank.yaml`, `e1_fixed_pool.yaml`** (end to end, Qwen). This is
PlugIR with only the representative choice changed, or only the pool schedule changed, run with
`agentir run ... --set data.limit=20`.

- *Counts as evidence:* BRI or Hits@10 improves over the `plugir` run on the same dialogs.

## 02. Are we keeping what the user actually told us?

**E2a: `e2_rewrite_drift_text.py`** (text only, 30 s). For PlugIR's released rewrites, the script
measures:

- how many of the details in the original description survive
- how often a rewrite drops a detail, or drops one and brings it back later
- colour contradictions
- rewrites that copy prompt text into the search query ("… among the retrieval candidates")

**E2b: `e2_rewrite_retrieval.py`** (no LLM, 5 min). The script measures:

- how often a rewrite pushes the target down
- whether rounds that leak text or drop a detail hurt more than other rounds
- whether text-only fixes recover rank: prepend the original description, strip leaked clauses

**E2c: `e2_replay_reformulators.py`** (Qwen, about 1 call per round). The questions and answers of
a finished run stay fixed. The script re-runs only the rewrite: PlugIR's prompt, a "grounded" prompt
(keep the original details, add only stated facts, never cite candidates), the same two with
anchoring, and plain concatenation. Any difference in BRI comes from the rewrite alone.

- *Counts as evidence:* anchored, stripped or grounded rewrites beat PlugIR's rewrite on BRI or
  Recall with the same Q&A.

## 03. Does a question chosen on 10 images help across the wider pool?

**E3: `e3_question_transfer.py`** (Qwen VLM, about 46 calls per dialog). The script uses three sets:

- **S:** A2R's DPP-10 over the top 50. The VLM sees S as a grid and writes 5 questions.
- **H1:** 15 other images from the top 100.
- **H2:** 15 more images from the top 100.

The VLM answers each question on every image. The simulated user answers on the target. The script
then compares:

- whether IG on S agrees with IG on H1 (Spearman correlation, and whether both pick the same top question)
- for questions chosen by S (A2R), by H1, by S+H1, at random, and by an oracle: how far the chosen
  question moves the target, and how much of H2 it rules out
- *Counts as evidence:* choosing by S+H1 moves the target further, or rules out more of H2, than
  choosing by S alone, and IG agreement between S and H1 is weak.

## Agentir changes behind these experiments

All of them are off by default, so the PlugIR and divergence fingerprints are unchanged
(`tests/test_experiments.py`).

- `generator.representative: entropy | rank` and `generator.pool_schedule: shrink | fixed`
- `reformulator.kind: grounded`, `reformulator.anchor_caption`, `reformulator.strip_leaks`
- Each round's debug now logs `context_indices`: the corpus ids of the images whose captions the
  questioner saw.
