# Checkpoint

The separate Codex session was interrupted before it ran inference. Work is running in the original chat. The local Qwen environment passed its host Metal calculation (1 + 2 = 3). The advancement rule is saved in advancement.md. Both development pilots are pending.

The ordinary MMR plus no-repeat control is complete on all 20 development IDs, matching the saved first-hit round on every ID. It scored 69% mean cumulative Hits@10 over R1–R5, 18/20 by R10, and 59 user questions. The unseen-evidence dialogue pilot is running. All new work uses run_history_pixels_pilot.py directly from this chat.

Development unseen evidence finished at 64% early score and 17/20 final hits versus 69% and 18/20. It gained 1111 and lost 343 and 2003. The initial question on 2003 changed from outfit color (“Blue”, immediate rank 2) to match versus practice (“unsure”, ten-turn miss). The single follow-up reserves two rejected contrast captions. Caption-only cutoff verification finished with no swaps and reproduced all control outcomes. The pixel cutoff arm is finishing.

Pixel cutoff verification finished. Among 18 eligible screens, images caused six swaps versus zero for captions. No swap gained or displaced a target. Dialogue 176's target occupied position 11 on the sand-color question. The simulated user called the sand brown, the candidate verifier called it gray on the same image, and all three incumbent candidates were labeled brown. In dialogue 1782 all six planes were labeled white. The alternative will use pixels to choose an unanswered question from the caption proposals when minimum KL chooses its generic fallback, with the same selector also run on captions. The history contrast pilot is now running, one GPU job at a time.

Development contrast finished at 60% early and 18/20 final, against 69% and 18/20. It recovered both unseen-only losses but first hit the food table in round 6 versus round 1 for ordinary evidence. It also gave up the unseen-only gain on the kitchen. Neither evidence change qualifies for confirmation. The alternate use of pixels in question selection is running against the caption and disabled controls. The confirmation trajectories remain reserved.

The alternate pixel question selector completed at 69% early, 18/20 final and 59 questions, exactly matching ordinary evidence. It accelerated dialogue 742 from round 6 to 1 and delayed 701 from 4 to 5 and 1639 from 1 to 5. The required improvement over disabled pixels was absent. The matched caption-question arm is still running. No development procedure has met the criterion for spending confirmation IDs.

The caption-only question comparator finished at 71% early versus 69%, final hits 18/20 in both, and 57 versus 59 questions. It accelerated two targets by one round. It is the only new procedure to pass development as a text-only selector. Its sources, settings, IDs and metrics are frozen in freeze.json before reading confirmation trajectories. The confirmation comparison is ordinary versus q-caption only.

**Complete.** Confirmation finished at 74% early for the frozen caption procedure versus 78% for ordinary, 20/20 final in both, and 53 versus 49 questions. All 180 arm-dialogue records passed validation. See report.md for the final decision. No live 400 was launched.
