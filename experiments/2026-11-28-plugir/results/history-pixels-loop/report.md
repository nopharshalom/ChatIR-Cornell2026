# AgentIR history and pixels pilot

**Keep ordinary MMR 0.85 caption evidence and minimum-KL question selection, with no-repeat user screens. No new 400-dialogue live run is recommended.** Both history changes reduced the primary early-retrieval score. Pixels changed displays and questions without a net gain. A caption-only question selector gained two percentage points on development, then lost four on independent confirmation. Both confirmation arms retrieved the same 20 targets by round ten.

## Matched outcomes

Early score is the mean of cumulative Hits@10 at rounds 1–5. Gains and losses are target identities at R10 relative to ordinary evidence on the same split. First-hit comparisons count a miss as later than any hit. Wall minutes sum measured per-dialogue execution in this session, including cache reads and any live regeneration. Each arm covers 20 complete or exactly replayed trajectories.

| Split | Method | Early score | R10 | Gained/lost | Earlier/later/tied | User questions | Wall min |
|---|---|---:|---:|---:|---:|---:|---:|
| Dev | Ordinary MMR | 69% | 18/20 | 0/0 | 0/0/20 | 59 | 0.03 |
| Dev | Unseen evidence | 64% | 17/20 | 1/2 | 8/6/6 | 72 | 17.07 |
| Dev | Eight unseen + two contrast | 60% | 18/20 | 0/0 | 5/7/8 | 70 | 16.03 |
| Dev | Caption cutoff check | 69% | 18/20 | 0/0 | 0/0/20 | 59 | 0.34 |
| Dev | Pixel cutoff check | 69% | 18/20 | 0/0 | 0/0/20 | 59 | 0.91 |
| Dev | Pixel question choice | 69% | 18/20 | 0/0 | 1/2/17 | 59 | 1.57 |
| Dev | Caption question choice | 71% | 18/20 | 0/0 | 2/0/18 | 57 | 6.88 |
| Confirm | Ordinary MMR | 78% | 20/20 | 0/0 | 0/0/20 | 49 | 0.02 |
| Confirm | Caption question choice | 74% | 20/20 | 0/0 | 0/2/18 | 53 | 3.31 |

The ordinary controls reused archived generation and needed only 1–2 seconds per split to verify and reconstruct the screens. The archived shared turns they used took 12.18 minutes on development and 9.73 on confirmation. All 40 history-variant trajectories were regenerated live. The pixel and caption cutoff checks added 52.5 and 18.7 seconds of candidate inference on development. The pixel question arm reused nine previously run matching trajectories, replayed unchanged decisions on ten, and regenerated one live. Its 17 scored proposal rows represent 49.1 seconds of grid inference. The caption question arm used 29.9 seconds for 17 scored rows on development and 46.0 seconds for 24 on confirmation. Cache history and early stopping affect timings, so the table reports actual pilot cost rather than a deployment speed comparison. Model loading is excluded.

## History and rejected screens

Removing all rejected IDs before choosing evidence increased first-round hits from eight to ten but dropped the R3 count from 15 to 12. It gained target 1111 and lost 343 and 2003 by the budget. In 1111, the first unseen-evidence question asked about the cabinets and got “white.” The new path eventually reached rank 9 at R10. In 343, ordinary evidence elicited “salad” and reached rank 6 at R1. Replacing all ten captions led to a question about the material of the table, “wood,” and a ten-round miss. In 2003, ordinary evidence elicited the woman's blue outfit and reached rank 2 immediately. Asking whether she was playing a match or practicing produced “unsure.”

The targeted follow-up kept the two nearest rejected captions from the current pool and filled the other eight slots with unseen MMR representatives. It recovered 343 at round six and 2003 at round two but lost the earlier kitchen gain. It found exactly the same 18 targets as the control, using eleven more questions. Its early score fell nine percentage points. The paired dialogue-bootstrap 95% intervals for early-score differences were −26 to +15 points for unseen evidence and −24 to +4 for contrast. These pilots support retaining the existing question evidence.

The saved **display-only** comparison on the original 400 is stronger. Hiding exact previously rejected screen IDs moved ordinary MMR from 328 to 342 hits at R1, 360 to 374 at R3, and 376 to 396 at R10. The mean R1–R5 score rose from 88.2% to 92.4%, and the same trajectories required 298 questions instead of 460. This replay preserves the original questions until a target is found earlier. It does not attribute the gain to changing how questions are generated.

## Pixels after an answer

Eighteen development screens had a qualifying single-color answer. The verifier classified the same six candidates at unseen ranks 8–13 using either their individual images or their captions. A positive outsider could replace one explicitly contradictory incumbent. The caption check made zero swaps. Pixels made six, retaining every true positive and adding zero targets. The six entering and six displaced candidate IDs are recorded in the screen traces. All 20 first-hit rounds matched.

The opportunity to change a target was narrow. On the ordinary trajectories, only case 176 had an eligible target just outside the screen, at rank 11. The user called the sand brown. The candidate verifier called that same image gray, and it classified all three incumbents as brown. Inspecting the photo showed gray-brown wet sand. On 1782, the at-risk target was at rank 10 and all six candidate airplanes were labeled white. In other cases the model could distinguish candidates even when the target was far away. For example, the kitchen floor check made a swap in 1111 at a target rank of 257.

## Pixels before the question

The second use of pixels kept five caption proposals and the prior indexed visual selector. Only a generic “other object” choice by KL could be replaced. Ten numbered images supplied predicted answers to supported proposals. The label split determined the question. The matched text arm substituted the same ten numbered captions. Both used the same no-repeat display policy, admissible proposals, vocabulary and pair-scoring rule. The target supplied one answer per turn.

On development, pixels changed 8 of 59 questions relative to the KL choice on their own trajectories. In 742, asking about a shirt elicited “black” and improved first hit from R6 to R1. In 1639, the ordinary generic question elicited “laptop” and reached the target at R1. Replacing it with shirt color elicited “white” and delayed the hit to R5. In 701, the sign-color question delayed the hit from R4 to R5. The five rounds saved in 742 were spent on the two delayed targets. Pixels tied disabled selection on the primary score and trailed the caption comparator by two points.

The caption comparator changed four questions in development. Dialogue 701 improved from R4 to R3 and 742 from R6 to R5. This was the only new procedure to pass the development rule. Its code, settings and the 20 disjoint confirmation IDs were frozen on disk before running it once on that sample.

On confirmation it changed three of 53 questions. In 1419 the generic answer “mountains” brought a snowboarder to rank 3 immediately. Asking whether he was on a snow-covered hill elicited “yes” and delayed the hit until R3. In 454 the generic answer “trees” was replaced by “no” to a question about a field without grass, delaying the hit from R2 to R4. The other 18 first-hit outcomes matched. The held-out paired early-score difference was −4 points, with a descriptive dialogue-bootstrap 95% interval of −10 to 0. There was no subsequent confirmation tuning.

## Cumulative Hits@10 counts

| Split | Method | R0 | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Dev | Ordinary MMR | 0 | 8 | 12 | 15 | 17 | 17 | 18 | 18 | 18 | 18 | 18 |
| Dev | Unseen evidence | 0 | 10 | 12 | 12 | 14 | 16 | 16 | 16 | 16 | 16 | 17 |
| Dev | Eight unseen + two contrast | 0 | 5 | 11 | 13 | 15 | 16 | 17 | 17 | 18 | 18 | 18 |
| Dev | Caption cutoff check | 0 | 8 | 12 | 15 | 17 | 17 | 18 | 18 | 18 | 18 | 18 |
| Dev | Pixel cutoff check | 0 | 8 | 12 | 15 | 17 | 17 | 18 | 18 | 18 | 18 | 18 |
| Dev | Pixel question choice | 0 | 8 | 12 | 15 | 16 | 18 | 18 | 18 | 18 | 18 | 18 |
| Dev | Caption question choice | 0 | 8 | 12 | 16 | 17 | 18 | 18 | 18 | 18 | 18 | 18 |
| Confirm | Ordinary MMR | 0 | 11 | 16 | 17 | 17 | 17 | 18 | 18 | 18 | 19 | 20 |
| Confirm | Caption question choice | 0 | 10 | 14 | 16 | 17 | 17 | 18 | 18 | 18 | 19 | 20 |

## Display and evidence exposure

Screen IDs were unique within each dialogue across all rounds, so screen slots equal distinct IDs summed by dialogue. The terminal winning screen also counts. Evidence slots are counted only when a question was asked. Distinct evidence is also summed within dialogues. Hidden representatives entered rejection history only if those IDs were actually displayed.

| Split | Method | Distinct displayed | Evidence slots | Distinct evidence |
|---|---|---:|---:|---:|
| Dev | Ordinary MMR | 790 | 590 | 354 |
| Dev | Unseen evidence | 920 | 720 | 587 |
| Dev | Eight unseen + two contrast | 900 | 700 | 537 |
| Dev | Caption cutoff check | 790 | 590 | 354 |
| Dev | Pixel cutoff check | 790 | 590 | 354 |
| Dev | Pixel question choice | 790 | 590 | 361 |
| Dev | Caption question choice | 770 | 570 | 346 |
| Confirm | Ordinary MMR | 690 | 490 | 330 |
| Confirm | Caption question choice | 730 | 530 | 351 |

## Reproduction and scope

Both frozen samples contain 20 initial misses from the original 400. The early and final rates in the pilot tables are conditional on an initial miss. The 400 already contain 290 initial hits. Each pilot uses the same 50,000-image corpus, five caption proposals, MMR weight, original model and seeds, and a budget of ten user questions. Simulated answers and rewrites can vary with question wording. The two small splits characterize the tested procedures and do not establish a population-wide effect size.

The working procedure, direct chat-terminal commands and resume behavior are in [commands.md](commands.md). The original plan and time-ordered decisions are in [advancement.md](advancement.md), [freeze.json](freeze.json) and [checkpoint.md](checkpoint.md). Source and input hashes are in [provenance.json](provenance.json) and each arm's plan. Each arm contains complete dialogue records. Its paired comparison JSON records first differences in screen, evidence and question, both rank sequences and target IDs gained or lost. Cached verifier records retain raw model output and ordered evidence IDs.

All 180 arm-dialogue records passed the checks on stop conditions, selected proposals, rewrite continuity, target membership, screen uniqueness, evidence policy, cached judgments and reconstructed hit curves. All 40 ordinary first-hit rounds matched the saved no-repeat replay exactly. The new runs used one GPU job at a time in the original chat. The only 400-dialogue figures in this report come from the pre-existing replay.
