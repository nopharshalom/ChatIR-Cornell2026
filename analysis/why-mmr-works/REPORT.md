# Why does MMR 0.85 work? (Nov 25, 2026)

This answers Matan's first request from his Nov 22 email: why replacing PlugIR's k-means selector with MMR helps, and whether the selected set is "diverse by chance" or better represents the data.

**Short answer: relevance, not diversity.** At λ=0.85, MMR is essentially "show the current top 10." What helps is no longer using PlugIR's k-means step. That step picks images from all over the candidate pool, mostly from the irrelevant tail. The questioner does better when it sees the images that are currently competing with the target, because those let it ask about the attribute that tells them apart.

Data: the matched 400-dialogue runs already in the repo (`experiments/2026-11-plugir/...` and `experiments/2026-11-22-plugir/results/...`). Same 400 targets, same initial captions, Qwen3.5-9B, frozen BLIP, the shrinking pool (500 → 50), and each run stops at the first hit. No new runs. Scripts are in this folder: run `python3 r0.py` then `python3 stats.py` (needs numpy, pandas, scikit-learn; CPU only). `mmr_why.png` is the figure.

## 0. What we changed (confirming Matan's reading)

Yes. Only the step that picks the 10 images whose captions go to the questioner changed:

- **PlugIR:** k-means into 10 clusters over the pool (top 500 − 50·round for the current query). From each cluster it takes the image whose caption has the lowest retrieval entropy.
- **Ours:** greedy MMR over the same pool. Relevance is the reciprocal of the current-query rank, min-max normalized. Redundancy is the max BLIP image-embedding cosine to images already picked. λ = 0.85.

Question generation, KL question selection, the answerer and the rewriter are unchanged.

## 1. What the improvement actually is

| 400 dialogues (paired) | Hits@10 by round 3 | Hits@10 by round 10 | Mean cumulative Hits@10, rounds 1–10 |
|---|---|---|---|
| k-means (PlugIR) | 87.3% | 94.0% | — |
| MMR 0.85 | 90.0% | 94.0% | — |
| Δ, 95% paired bootstrap CI | **+2.7 pp [+0.3, +5.2]** | **+0.0 pp** | +1.3 pp [−0.6, +3.3] |

MMR reaches the target sooner. The gain shows up in rounds 1–5. It does not find more targets by round 10. The paper should claim "fewer questions to a hit," not "higher final Hits@10."

## 2. MMR at 0.85 is almost exactly top-10

| Selector | Shown images from the pool's top 10 | Mean pool position of shown images |
|---|---|---|
| k-means (PlugIR) | **0.3 / 10** | **~180 / ~300** (all rounds); ~270 / 500 at round 0 |
| MMR λ=0.85 | **9.9 / 10** | 4.5 |
| Top-10 (λ=1) | 10 / 10 | 4.5 |

- **Matched first round:** at round 0 the pool is identical across runs. There, MMR 0.85 showed exactly the same 10 images as top-10 in **55 of 62** dialogues.
- **Why:** relevance drops only about 0.14 from rank 1 to rank 10, and λ=0.85 leaves the redundancy term only 0.15 weight. An image outside the top 10 therefore has to be far less redundant to displace one inside it, which almost never happens.
- **Top-10 does as well as MMR 0.85 (sample A):** +1.25 pp area vs k-means; MMR 0.85 minus top-10 = −0.1 pp [−2.4, +2.2].
- **Adding diversity does not help:**

| Selector | Δ area vs k-means | Sample |
|---|---|---|
| λ=0.7 | +0.5 pp | A |
| λ=0.7 | −0.8 pp | B |
| λ=0.5 | −1.0 pp | B |

  All of these intervals cross zero.
- **Noise floor:** in the 55 matched round-0 cases with *identical* evidence sets, 16 still produced a different question and next rank. So differences between λ values are inside run-to-run noise. Do not present 0.85 as a tuned optimum.

## 3. "Diverse by chance, or representing the data better?"

Neither one. The k-means set *is* a good summary of the pool's diversity. That is the problem.

- **Where k-means picks from:** its 10 images are spread almost uniformly over pool positions (see `mmr_why.png`, left). The lowest-entropy caption in each cluster is unrelated to relevance, so 9+ of the 10 captions describe images far from the query. Examples: tennis players, trains and horses shown for a "white computer mouse on a mouse pad" query.
- **k-means is more diverse but covers the wrong region:** more distinct content words (32 vs 22). In caption space (TF-IDF), its coverage of the pool's top-50 neighbourhood is 0.42, against 0.60 for MMR, and its similarity to the target's caption is 0.43 vs 0.52 (matched round 0: +0.11 [+0.07, +0.15]).
- **Where the target sits:** whenever a question is asked, the target is outside the top 10 by definition. At round 0 its median pool position is 35. So the current top 10 is precisely the set the target has to overtake: the hard negatives.

**Mechanism.** The questioner writes questions about what differs among the captions it sees.

- **Near-neighbour captions (MMR/top-10):** the varying attribute is the one that separates the target from its competitors, so the answer pushes competitors down.
- **Pool-wide captions (k-means):** the varying attribute is "what kind of scene is this." The query already answers that, so the question is redundant or rests on a false premise. Unsure or negative answers on the matched first question: k-means 9.1% vs MMR 3.6%.

On the matched first question (110 dialogues that need a question), the target reaches the top 10 after one answer in 34.5% of MMR dialogues vs 27.3% of k-means ones: Δ +7.3 pp [−1.8, +17.3]. It is a hit only under MMR in 19 dialogues and only under k-means in 11. The direction is consistent but the sample is small.

## 4. Concrete examples (matched first question, same query and pool)

**qid 1772.** Query: "a white computer mouse sitting on top of a mouse pad". Target: orange mouse pad. Starting rank 17.
- **k-means captions:** "a woman in a white dress on a tennis court", "a white and yellow train…", "a very clean toilet…", "a woman riding a white horse…", plus a few desk items.
  → *"what is the other object in the image?"* → "keyboard" → rank 24.
- **MMR captions:** "a white computer mouse", "a computer mouse on a desk", "a keyboard with a mouse", "mouse on bed", …
  → *"what color is the mouse pad…?"* → "orange" → **rank 2**.

**qid 1874.** Query: "a bus passes so close to a pick up truck that it catches the truck's mirror". Starting rank 13.
- **k-means:** generic city-bus captions, plus a dirt road with cows.
  → *"what surface is the bus driving on?"* → "concrete" → rank 18.
- **MMR:** "side view mirror", "a red bus and a military truck", "view of the rear of a truck", …
  → *"What is the color of the truck?"* → "blue" → **rank 1**.

**qid 93 (doubles tennis).**
- **k-means:** singles players, crowds, and "a group of people on bicycles in a stadium".
  → *"Who is playing doubles with the two men?"* → "no one" → rank 19.
- **MMR:** "two men playing tennis", "three men playing tennis", …
  → *racquet colour* → **rank 7**.

**qid 767.** MMR's set contained the target itself ("a man with a balancing board on a lamp post").
→ *"what type of pole…"* → "street light" → **rank 4**.
k-means asked the colour of the fire hydrant → rank 17.

**Where it breaks (k-means wins, 11 cases).** When all 10 captions are near-duplicates, the questioner sometimes asks about an attribute they all share, so the answer separates nothing:
- **qid 360:** plate colour → "white" → rank 27 → 81. k-means asked the type of food → "sandwich" → rank 3.
- **qid 508:** shorts colour → "white". k-means asked where the match is → "Wimbledon".
- **qid 570:** number of clocks → "1".

**Takeaway:** some scene-level context still helps. That is the opening for a selector that is relevance-first but makes sure some *attribute* varies across the set, rather than raw embedding diversity.

**Caveat:** four of MMR's first-question wins (qids 842, 482, 1639, 627) came through the generic "what is the other object?" fallback. The improvement is not entirely due to better-grounded questions.

## 5. How to frame it in the paper

Don't frame it as "MMR adds diversity." Frame it as: **"the questioner should see the target's current competitors, not a summary of the candidate pool."**

- PlugIR's clustering is relevance-blind. Its choice of shown images is statistically close to uniform over the pool.
- Replacing it with relevance-ranked evidence makes questions discriminative among the hard negatives. Target found sooner: +2.7 pp at round 3.
- Extra diversity (lower λ) doesn't help.

This also reframes the rest of Matan's list:
- **"10 vs 5 questions":** with relevant evidence, more candidate questions may now be worth having.
- **Image grid to the questioner:** show the top-10 competitors as images, not a diverse grid. Captions drop exactly the discriminating details (see the universal-failure audit).
- **MMR on the final top-10 baseline:** this predicts that it will hurt quickly. It would push relevant images out of the user-facing list.

## 6. Next steps to make this paper-grade

1. **Re-do the coverage analysis in BLIP image-embedding space.** Measure pool coverage, top-50 coverage and target-to-nearest-shown cosine. The analysis above uses caption TF-IDF because `cache/corpus_blip.pth` isn't in the repo. It needs the Mac or Colab where the cache lives.
2. **Run a "random 10 from the pool" control** next to k-means. If it matches k-means, that shows PlugIR's clustering adds nothing over chance. The Nov 11 E1 result showed similar target exposure for k-means (4.0%) and random (5.8%).
3. **Treat λ as noise.** Report top-10 and MMR 0.85 together, or tie λ to the run-to-run noise floor (16 of 55 identical-evidence cases diverge).
4. **Try an "attribute-diverse top-K" selector** aimed at the k-means-win cases (shared-attribute questions).
