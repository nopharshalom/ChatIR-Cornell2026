# AgentIR: combined 500-dialogue diagnostic audit

17 November 2026. All comparisons below treat the saved dialogues as one matched sample of 500. This is an exploratory diagnosis of existing runs, not a new end-to-end evaluation.

## What was checked

- Validated 2,000 completed records: identical dialogue IDs, targets, initial captions and initial ranks across vanilla, S2 all-query RRF pooling, S3 current MMR, and probe selection.
- Audited all 87 variant-versus-vanilla final-outcome disagreements, covering 54 distinct dialogues. Every pair has a signal profile, qualitative notes, and complete saved traces in the accompanying files.
- Compared the first differing action from identical preceding dialogue histories: 95 S2 pairs, 131 MMR pairs and 126 probe pairs. A subset of 99 probe pairs also has identical candidate questions and evidence.
- Inspected all six images missed by every method, their complete traces, and their stored captions. Also inspected images 106, 343, 1399 and 1406 to verify specific apparent errors.
- Ran 22 CPU retrieval replays with frozen BLIP, the original 50,000-image embeddings and tokenizer settings. These include handwritten query edits, target-caption queries and explicitly privileged image-assisted descriptions. All 22 control ranks exactly reproduce the saved ranks.
- No new Qwen answers or full dialogue rollouts were generated. Metal was unavailable in this tool session. Matched observed branches provide the question comparison; newly encoded text provides the rewrite ablations.

## 1. Help and harm mostly cancel at the aggregate level

364/500 targets are already in the top 10 before questions. The behavioural comparisons concern the remaining 136 dialogues.

| Method | Hits by r1 | Hits by r2 | Hits by r5 | Hits by r10 | Rescues vanilla failures | Loses vanilla successes | Questions used |
|---|---:|---:|---:|---:|---:|---:|---:|
| Vanilla | 400 | 420 | 451 | 468 | — | — | 646 |
| S2 all-query pool | 400 | 425 | 455 | 468 | 14 | 14 | 621 |
| S3 current MMR | 404 | 421 | 452 | 468 | 16 | 16 | 634 |
| Probe selection | 400 | 422 | 451 | 469 | 14 | 13 | 640 |

S2 saves 25 questions (3.9%) with the same final hit count. Question use is measured until first success or the ten-question limit; it is not inference cost, since probes perform extra work per question. Across all methods, 494/500 dialogues succeed somewhere; six fail everywhere. That union is an oracle diagnostic requiring knowledge of which method will work. It is not a deployable 98.8% method and combines multiple search trajectories and budgets.

The paired bootstrap intervals remain broad: S2's +1.0 pp at r2 has an exploratory 95% interval of [-1.0,+3.0] pp; probe's +0.2 pp at r10 has [-1.8,+2.2] pp. These do not establish a reliable aggregate improvement.

### Dialogue categories

Coarse groups assigned from words in initial captions, among the 136 initial misses:

| Group | N | Vanilla recovered | S2 | MMR | Probe |
|---|---:|---:|---:|---:|---:|
| Animals | 17 | 13 | 10 | 12 | 12 |
| Food | 10 | 9 | 10 | 10 | 8 |
| Interiors | 23 | 16 | 17 | 14 | 14 |
| Sports | 27 | 18 | 17 | 18 | 20 |
| Transport / street | 24 | 18 | 19 | 19 | 19 |
| Other | 35 | 30 | 31 | 31 | 32 |

There is no strong basis here for a scene-category switching policy. For example, probe helps two net sports cases but loses two net interiors cases; these are small, exploratory groups. Mechanisms in the dialogue provide more useful experimental hypotheses than the broad categories.

### Repeated and low-information questions

| Method | Failed dialogues | Failures with repeated question | Exact generic-object question selections, all turns | Those with another candidate available |
|---|---:|---:|---:|---:|
| Vanilla | 32 | 22 | 170 | 169 |
| S2 | 32 | 27 | 150 | 149 |
| MMR | 32 | 26 | 178 | 175 |
| Probe | 31 | 20 | 78 | 77 |

Repetition uses lowercase/punctuation-normalized exact wording, so it misses paraphrases. Failed dialogues last longer and therefore have more opportunities to repeat; these counts are not causal estimates.

The generic question is `what is the other object in the image?`. The archived generation/filter loop contains this exact fallback. The saved candidates show that selection usually chooses it while alternatives exist, rather than being forced to use a wholly collapsed candidate set. Its provenance cannot be inferred with certainty on every occurrence because raw pre-filter questions were not preserved.

Probes reduce generic questioning, but shift toward colour questions: 334/640 (52.2%) versus vanilla's 235/646 (36.4%). Diversifying hypothetical colour answers can change retrieval distributions without necessarily asking the most distinguishing question. That is a plausible explanation to test, not an established cause of the score difference.

### Qualitative mechanisms worth pursuing

1. **Distinctive details help.** Dialogue 722: red kite takes probe from rank 81 to 4 in one question; vanilla never hits. Dialogue 56: MMR obtains night and moves 18 to 9. Dialogue 2029: S2 asks setting and obtains river, succeeding in two questions. Other successes obtain team/venue, airline, background or object-count details.
2. **Question assumptions leak into queries.** Dialogue 106 introduces a cat from a sink-colour question while the answer only says white. The target image has no cat. Dialogue 343 introduces sandwich/pizza through plate-colour questions; neither appears in the target image. Dialogue 1732 turns one tennis player into two while resolving descriptions of an airborne player.
3. **Correct facts can be attached to the wrong entity or relation.** Dialogue 1648: a hill behind the zebras becomes zebras grazing on a hill. Dialogue 1676: people holding bikes become people riding bikes. This calls for preserving relations, not only retaining keywords.
4. **Simulator errors are a separate contributor.** In universal failure 310, the simulator repeatedly answers black to questions about nonexistent cats. A rewriter that faithfully incorporates every answer can still fail. Presence checks, contradiction handling and evaluation of the simulator need separate consideration.
5. **Initial captions are not infallible.** Image 1399 is a double-decker bus, despite the initial trolley caption. MMR corrects this and succeeds. Image 1406 shows traffic signals rather than the initial flags; vanilla uses a corrective answer and succeeds. A fact ledger should retain provenance and allow corrections, not freeze the initial caption as unquestionable truth.
6. **Some successes occur after poor questions.** Dialogue 2057 succeeds after a malformed candidate-image question elicits a useful scene description. Dialogue 1640 succeeds after a malformed question elicits blue cutting board. Dialogue 767 gets skinny in response to a colour question. Outcome alone is not a clean label of question quality.

Every discordant pair is included in `discordant_pairs.csv`. Tags are nonexclusive observations across the relevant dialogue's runs, not adjudicated causal labels. The review covers successful final Q/A, largest losing deterioration and repetition profiles for all pairs; full-history interpretation and image verification are deeper for the selected examples above.

## 2. First divergence and fixed-query replays

### Comparisons at an identical starting history

Each row compares the first differing Q/A/rewrite action; all earlier actions and the current query are identical. For S2/MMR, evidence and candidate questions change together, so this measures the evidence-to-question pathway rather than an isolated selector score.

| Comparison | Pairs | Variant gives better next rank | Worse | Tied | Immediate new hits / lost hits |
|---|---:|---:|---:|---:|---:|
| S2 vs vanilla | 95 | 46 | 37 | 12 | 15 / 11 |
| MMR vs vanilla | 131 | 61 | 60 | 10 | 18 / 14 |
| Probe vs vanilla, all first divergences | 126 | 71 | 41 | 14 | 20 / 16 |
| Probe, identical evidence AND candidates | 99 | 53 | 34 | 12 | 15 / 15 |

All 99 strictly matched probe comparisons occur at the first question. They isolate the selected question more closely than the full trajectories, although answer/rewrite generation is still part of the measured outcome. They condition on a differing selected action and do not constitute a new randomized experiment.

Probe more often improves the continuous rank in these 99 comparisons, yet creates and loses exactly 15 immediate top-10 hits. A better rank is not automatically a better Hits@10 outcome. Of probe's 13 final losses against vanilla, seven initially improve the target's rank relative to vanilla at first divergence and still eventually fail. This supports examining downstream accumulation of errors as well as the initial choice.

### Handwritten rewrite ablations

The following are newly encoded queries on the same frozen corpus. They do not regenerate answers or evaluate a deployable rewrite policy. Cases were selected after seeing their traces.

| Dialogue | Edit | Recorded/control rank | Edited rank |
|---|---|---:|---:|
| 106 | Remove unconfirmed cat clause | 93 | 40 |
| 343 | Remove unconfirmed sandwich, retain white plate | 517 | 212 |
| 161 | Remove invented second moving train | 43 | 16 |
| 161 | Only reverse green/yellow order | 14 | 8 |
| 1542 | Remove unconfirmed fireplace clause | 70 | 19 |
| 1894 | Omit `without a fireplace` after denial | 230 | 131 |
| 1648 | Put hill behind zebras rather than zebras on hill | 151 | 50 |
| 675 | Remove frisbee and extra player clauses | 562 | 345 |
| 310 | Remove nonexistent cats, image-verified correction | 262 | 90 |
| 853 | Remove second person, image-verified correction | 25 | 43 |

Most edits improve rank, but almost none alone create a hit. One correct-looking cleanup worsens rank. The train word-order example crosses the threshold without adding information. Together these show both semantic contamination and embedding sensitivity; they do not prove that a general cleanup policy will improve Hits@10.

## 3. The six universal failures

For each, one short description was written after inspecting the target image, then searched once. This is explicitly privileged use of target information, intended only to ask whether BLIP can retrieve the image at all. A second oracle query uses the existing stored target caption directly.

| ID / scene | Best rank across all saved methods | Stored target-caption query rank | Image-assisted query rank | Diagnosis |
|---|---:|---:|---:|---|
| 310: computer desk | 15 | 394 | 1 | Nonexistent cats contaminate three trajectories; maps/papers on walls are distinctive but unasked. |
| 675: baseball catcher | 217 | 28 | 1 | Target never enters any evidence pool. Queries focus on catcher; batter, umpire and crowd describe the image more effectively. |
| 786: tennis player | 11 | 208 | 1 | Vanilla/S2 ask the identical generic question ten times. MMR nearly hits. Serving pose, sleeveless shirt and court branding are missed. |
| 853: skier | 22 | 83 | 2 | Skier/snowboarder confusion and entity duplication; ramp, flags and mountain setting are more distinguishing. |
| 1648: zebras | 151 | 30 | 1 | Repeated zebra/colour answers; background herd and wooded hill are largely missed. Hill relation is also rewritten incorrectly. |
| 1743: bedroom | 45 | 2 | 1 | Bed/wall colours dominate; brick wall, windows and rug are missed. Stored caption is already useful but never selected as target evidence. |

The target enters an evidence pool at some point for five of these six cases. Among the six, only the skier's target caption is ever shown, in the probe run. Its stored caption is simply `the mountain is blue`.

Other stored target captions include `a large field` (zebras), `people watching a baseball game` (catcher), and `the wooden desk has many accessories on it` (desk). Caption quality can discard precisely the details needed for questions. Merely selecting a target image's existing caption is therefore not equivalent to exposing its visual information.

The image-assisted queries retrieving all six at ranks 1–2 show that these particular images are retrievable with more suitable descriptions. They do not establish the best achievable general performance, nor that an agent could discover those details within ten questions.

## What this suggests testing next

**First: separate question novelty from grounded rewriting.** Run two controlled ablations before combining them:

- A novelty-aware question selector: reject semantically already-answered questions, including the generic fallback when it repeats an answer; regenerate if necessary. Permit explicit correction of doubtful existing facts. Keep retrieval, evidence and rewrite unchanged.
- A grounded rewriter: track entities, relations and fact provenance; distinguish an answer's confirmed attribute from a question's unconfirmed premise; handle uncertainty/denial without adding spurious objects or duplicate entities. Keep question selection unchanged. Include inconsistent simulator answers as a separate diagnostic.

The first tests whether wasted questions are recoverable opportunity. The second tests whether the rank improvements in handwritten ablations generalize. Evaluate full trajectories on the same 500 with paired gained/lost counts and question cost, then validate any selected method on untouched data. The 364 initial hits can still stop immediately under the existing protocol.

**Then: make probes assess grounded, novel information.** Add absent/unclear possibilities to hypothetical answers where appropriate, and compare the resulting factual rewrites rather than only appending hypothetical Q/A. Test whether high divergence comes from plausible distinguishing information or merely easily separated colours. Do not use target ranks as an online signal.

**For the broader AgentIR direction: inspect retrieved images and search alternative interpretations when stuck.** The six hard cases motivate richer evidence (image grids or focused descriptions of retrieved candidates), new attributes, and alternative scene interpretations. An agent could use repeated-answer detection or contradictions as observable triggers. The 494/500 oracle union motivates this research but does not establish a usable switching rule.

## Reproduction and files

From the project directory:

```bash
ChatIR-Cornell2026/.venv/bin/python ChatIR-Cornell2026/scripts/analyze_500_dialogues.py
ChatIR-Cornell2026/.venv/bin/python ChatIR-Cornell2026/scripts/replay_audit_queries.py
```

- `sources.json`: all input paths and SHA-256 digests.
- `summary.json`, `curves.csv`: aggregate outcomes, paired bootstrap intervals and diagnostics.
- `dialogues.csv`, `turns.csv`: complete computed per-dialogue and per-turn measures.
- `discordant_pairs.csv`, `review_notes.json`: all 87 discordant pairs, plus qualitative notes for all 54 distinct dialogues.
- `first_divergence.csv`: all 352 comparisons at the first differing action.
- `trace_audit.md`: full saved traces for every discordant pair and universal failure.
- `scene_groups.csv`: coarse lexical scene breakdown.
- `query_replay_specifications.json`, `query_replays.csv`, `query_replay_metadata.json`: exact handwritten/target-caption query interventions, results and CPU control validation.

The saved dialogue source files are unchanged. The scripts read existing outputs and write only this analysis directory. The manually authored review notes and this report are preserved when the analysis script is rerun.
