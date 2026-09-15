# Safety-accounting correction

The frozen final report is retained for provenance. Its safety table incorrectly interprets the stored total attempt count as a retry count (>0 instead of >1). Consequently every slot is labelled retried. The safety figure also sums logical method attributions of shared BO initialization and calls all attempts non-training infrastructure attempts. Do not reproduce that table's retry column or that figure's caption in the thesis.

Use safety-accounting-correction.csv: 655 physical slots, 657 total attempts, 655 completed attempts, two retained failed attempts, and two retried slots. The failed attempts are DATASET_RESTORE_FAILED and HOST_POWER_INTERRUPTION. Both concern shared BO initialization; each BO method therefore inherits two retry attributions, but summing these to six would triple-count physical events. Random, Sobol, and default controls have zero retries.

Replacement safety-figure caption: All 655 physical observations completed validly. Two pre-completion infrastructure failures were retained and retried on the same candidates without consuming extra candidate slots. Shared BO initialization is counted once in physical totals.

This is a post-result reporting correction derived from authenticated attempt rows. No observation, endpoint estimate, permutation test, method ranking, source analysis hash, or deployment authorization is changed. The original renderer remains reproducible; this companion is required when citing its safety accounting.
