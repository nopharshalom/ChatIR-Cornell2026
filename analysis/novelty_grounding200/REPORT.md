# Novelty and grounded rewriting: completed 200-dialogue audit

Both runs completed. All 200 IDs and initial ranks match the local vanilla reference. Metrics below are cumulative Hits@10 by question round; runs stop at the first hit. The 138 initial hits do not exercise either intervention. There are 62 active dialogues per arm.

| Method | Round 1 | Round 2 | Round 5 | Round 10 | Final delta |
|---|---:|---:|---:|---:|---:|
| Vanilla | 75.5 | 79.0 | 88.5 | 93.5 | — |
| Novelty | 76.5 | 79.5 | 86.5 | 89.5 | −4.0 pp |
| Grounded rewrite | 74.5 | 81.0 | 86.5 | 91.0 | −2.5 pp |

Novelty rescues 3 vanilla failures and loses 11 vanilla successes. Grounding rescues 3 and loses 8. Of the 62 initial misses, vanilla recovers 49, novelty 41, and grounding 44. Processing times were approximately 75, 108, and 81 minutes respectively. These times describe these runs, not hardware-controlled benchmarks.

## What was examined

Reviewed all 25 method–dialogue pairs with discordant final outcomes, including the full question/answer/query trajectories; checked the additional 10 shared failures per variant; inspected judge decisions at informative novelty divergences; computed repetition, regeneration, fallback, and unchanged-query statistics across every turn. The trace export includes all active trajectories. This audit uses saved language and rank evidence, not fresh visual adjudication of every target or counterfactual model reruns. A rank improvement does not prove a statement is visually true.

## Novelty: it removed repetition, but the filter was unreliable

- Exact normalized repeated questions fell from 52/305 vanilla turns to 1/324 novelty turns. The generic “what is the other object” question fell from 72 to 19 selections. Novelty therefore did change its intended behavior, but did not improve retrieval overall.
- 27/324 initial judge responses were invalid JSON or otherwise failed parsing; across initial and regenerated batches, 33/427 judgements were invalid. Regeneration occurred on 87 turns and final fallback to original candidates on 9 turns.
- A parse failure currently behaves like an empty accepted set and triggers regeneration. Three of the 11 final losses first diverged on such a failure: 297, 748, 2057. This is an implementation confound, not evidence that novelty itself caused those losses. The 384-token budget and verbose reasons produced truncated responses in examples; malformed quoting also occurred.
- Valid JSON did not guarantee a valid decision. In 1091, the judge rejected jet-colour questions despite no known colour, allowing a leading grey-versus-black-jet question. In 1109 it rejected toilet colour while explicitly acknowledging it was unknown, and retained a grab-bar question. In 1676 it rejected all original questions, including what people were holding; vanilla later obtained “bikes” and succeeded.
- Deduplicating candidate paraphrases also changes which wording reaches KL scoring. This experiment is more than a filter against previously answered questions.

### Every discordant novelty case

| ID | Outcome | What the trajectory shows |
|---|---|---|
| 35 | Loss | Judge confuses tree species “alder” with a known location and regenerates feather questions; added detail yields ranks 12→28. Vanilla later obtains leaves and hits at round 6. |
| 69 | Loss | Rejects terrain refinement because “open field” is already known. Takes lighting/background route; vanilla obtains dirt, zoo, then logs and hits. |
| 297 | Loss | Invalid first judgement triggers regeneration. Its incomplete text also treats living-room identity as settled; vanilla explicitly corrects it to office. Novelty remains on a living-room trajectory. |
| 532 | Loss | New questions about whom the player faces and current action lead to a pitcher-plus-batter description. Vanilla eventually describes a batter and crowd and reaches rank 10. |
| 573 | Loss | Blocks redundant daytime question, adds car and many later attributes. Vanilla's shorter yellow-light/mountain description reaches 9; novelty never beats 12. |
| 748 | Loss | Invalid first judgement replaces candidates with a flower-presence question. Later umbrella answers differ (white in vanilla, black in novelty), showing simulator/wording sensitivity as well as different questions. |
| 896 | Loss | Accumulates rain, negative clock-tower statements, and extra details. Vanilla's generic question receives a rich scene description with people, scooters, signs and reaches rank 1. |
| 955 | Gain | Concrete ledge and black-and-white image questions offer useful new information. The black-and-white answer accompanies rank 210→36; a subsequent no-hat answer/rewrite reaches 5. Attribution to one fact alone is untested. |
| 1091 | Loss | Valid but wrong rejection of unknown jet colour leads to assumed grey/black jets and entity confusion. Eventually approaches rank 11 without a hit. |
| 1109 | Loss | Rejects unknown toilet colour and asks about grab bars. Never obtains vanilla's water-heater detail; later query duplicates the toilet. |
| 1676 | Loss | Rejects original holding-object question, regenerates weather/surface alternatives. Later misbinds traffic-light red to crosswalk red. Vanilla gets bikes and hits at round 2. |
| 1894 | Gain | Different route gets brown flooring and fabric couches and reaches 10. “No dog” remains in the query; this is not evidence that unsupported/negative clauses were eliminated. |
| 2029 | Gain | Location wording gets “river,” reaching rank 3 at round 3. Vanilla asks a similar, more specific location question, gets an uninformative answer, then repeats elephant questions. |
| 2057 | Loss | Invalid judgement at round 2 replaces sign-colour candidates with pole questions. Vanilla's later malformed question happens to elicit parked cars and succeeds; cleaner questions need not produce better simulator answers. |

## Grounded rewriting: mixed local effects, no reliable factual guarantee

All 62 active dialogues began with the same question and answer as vanilla. After that first rewrite, target rank was better in 19, worse in 17, and tied in 26. This is a useful matched comparison before later trajectories diverge; it does not measure visual correctness. No grounding fallback occurred.

- Grounding retains the original questioner, so repetition persists: 53/330 repeated questions versus 52/305 for vanilla. Generic-other-object selections remain 72.
- It produces 56 exactly unchanged queries; vanilla has zero because its implementation forces a paraphrase when reconstruction is unchanged. Therefore this arm changes both rewrite instructions and the unchanged-query policy. The observed loops cannot be attributed solely to factual grounding.
- Some corrections work: 532 replaces “on the mound” with “home plate” after an explicit answer; 332 ignores an unsure location answer and later reaches rank 7 with yellow grass. But improved handling of one answer does not ensure final success.
- Major intended safeguards still fail: in 1288, “what colour is the giraffe near the wooden bridge?” / “brown” introduces a wooden bridge and an unsupported count of two giraffes. Later the query expands to three giraffes. In 1399, trolley/bus descriptions become two vehicles; in 853 the final description is a snowboarder with skis.
- More details do not consistently help the frozen retriever. In 1820 the query is rank 11; a detailed fixture answer produces a longer description at rank 25. Later details do not recover. This is observed association, not a replay isolating query length or any one token.

### Every discordant grounding case

| ID | Outcome | What the trajectory shows |
|---|---|---|
| 69 | Loss | Dirt/zoo description gives way to repeated giraffe/background questions; misses vanilla's logs detail. Several queries remain unchanged. |
| 297 | Loss | Corrects living room to office, preserves bookcase/computer, but loops on desk/bookcase colour; vanilla eventually gets chandelier and succeeds. Cleaner intermediate text is insufficient. |
| 532 | Loss | Corrects mound to home plate, but later contradictory hat answers change the description; does not obtain vanilla's crowd detail. |
| 955 | Gain | Gets concrete ledge at round 2 (341→37), reaches 7 by round 6 with a different scene description. Does not establish that grounding alone rescued a particular factual error. |
| 1109 | Loss | Diverges after an initially identical three-round trajectory. Never asks about the water heater/sink that drove vanilla success; ends at 394. |
| 1169 | Gain | Moves toward a monitor/laptop description, black desk and green monitor; reaches 2. Vanilla remains at best 11. Still contains premise-derived details, so this is not a clean grounding demonstration. |
| 1288 | Loss | Question premises introduce basket/bridge and entity-count drift despite grounding instructions. Vanilla asks what giraffes eat from, gets wall, and hits. |
| 1510 | Loss | Collects shorts, shoes, ball, socks, racket and hand details; vanilla gets black shirt plus crowd and hits at round 3. Grounding never beats 25. |
| 1820 | Loss | Remains at rank 11 after a repeated answer, then accumulates fixtures/negative sink text. Vanilla's paraphrase changes the trajectory and an open-toilet answer reaches 6. |
| 2029 | Gain | Asks location at round 2, gets river, reaches 3. Shares the useful scene-setting feature of the novelty gain. |
| 2057 | Loss | Adds sky/building/pole colours and types without the parked cars that vanilla's unusual question elicits. |

At round 2 grounding gains eight early hits and loses four; seven of those eight early gains are dialogues vanilla eventually also solves. The early gain is mainly speed of finding existing successes, rather than new final recoveries.

## What this adds to the previous 500-dialogue analysis

The prior full-500 tests had S2 all-query pooling at +1.0 pp by round 2 and 0 pp by round 10; S3 current MMR at +0.2 pp by round 2 and 0 pp by round 10; divergence probing at +0.4 pp by round 2 and +0.2 pp by round 10. These new 200-dialogue results do not identify a stronger default module.

They narrow the next experiment: indiscriminate novelty can suppress useful clarifications, the proposed grounding prompt does not enforce its own constraints, and unchanged query states can persist with unchanged question behavior. Useful recoveries frequently involve a specific scene relation, setting, or image style, but colour and generic object questions also sometimes work well. There is no supported universal rule to avoid those categories.

## Proposed next experiment: use searches to escape a repeated-information loop

Keep vanilla as the comparison. A concrete trigger available to the agent is two consecutive identical normalized question/answer pairs, while questions remain. On saved vanilla traces this triggers in 16/200 dialogues: 7 of the 13 final misses and 9 eventual successes. A seventeenth dialogue (273) hit at the repeat itself and had no next turn. This is a useful measurable opportunity and a real regression risk. The seven reachable vanilla misses impose a retrospective maximum of +3.5 pp if all are rescued and no successes lost; it is not an expected gain.

At a trigger, issue two or three searches that preserve confirmed main entities while varying an unresolved setting, relation, or scene identity. Use returned captions to propose a question about the distinction, with an open answer/“neither” option. The search results are hypotheses, not confirmed target facts. Feed the actual answer into the baseline rewrite. Initially allow one intervention per dialogue.

The essential comparison is a triggered history-aware new-question arm without extra searches versus the same intervention supplied with probe results, alongside vanilla. Match the question budget and generation settings, record retrieval/LLM costs, and avoid claiming a probe benefit from extra generation alone. Prototype on saved trigger states to check whether probes produce useful distinctions, then test end to end because changing a question changes all subsequent states. Use these 200 as development data; freeze the design before evaluating on untouched IDs outside the inspected 500.

Before reusing the novelty judge, fix parse failure handling (preserve original candidates on judge failure), shorten/structure its output, and verify judgement quality on examples. A full rerun of the same filter is not the highest-priority next direction. Do not combine both losing variants or assume S2 must be the foundation.

## Reproduction and limits

Run `python3 scripts/analyze_novelty_grounding.py` from the repository, with the existing sibling `output/` directories present. It validates ID sets and initial ranks and writes metrics, source hashes, discordant pairs, and complete active traces. This report supplies the qualitative review; the script does not automatically adjudicate claims.

No new model experiments were launched. No counterfactual outcome is inferred from a selected trace. Post-divergence comparisons mix changed questions, answers, and rewrites. The simulator can give different answers to near-paraphrases. Early stopping prevents end-round recall and post-hit drift analysis. Repeated inspection makes these dialogues development evidence; improvements require subsequent evaluation on untouched examples.
