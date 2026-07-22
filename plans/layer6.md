# Layer 6: the eval gate (the judge)

Design plan, agreed in conversation. Read after PLAN.md (the why) and STATE.md
(the situation).

## Context

Validation proves code runs. The reviewer finds code smells. Neither catches a
patch that runs, passes the tests, and is still wrong or incomplete. The judge does:
it reads the produced code and decides whether it meets the acceptance criteria that
no exit code can verify, the semantic ones a human wrote and only judgment can check.

Per PLAN.md the gate sits before the expensive review-and-fix stretch, so one cheap
judge call can stop bad work before it triggers several reviewer and fixer calls at
full context. A correctness gate that is also the cheapest cost control in the
system. `role="judge"` and `attempt_kind="eval"` were reserved in the seam from
Layer 0, so cost tracks by role for free.

## Decisions locked (with the user)

- **Grade the ticket's semantic acceptance criteria** (the `Criterion`s with no
  executable `check`), not a free-form "is this good" score. Grounded in what the
  human asked, off the reviewer's turf, and a no-op that says so when a ticket has
  none.
- **Per-criterion three-state verdict** (satisfied / not_satisfied / uncertain),
  each with a reason. The gate passes only if all are satisfied; uncertain counts as
  not-passing, loudly. Mirrors `checks.py`'s SATISFIED/PROCEED/BROKEN.
- **A documented deviation from PLAN.md.** PLAN.md says "judge scores the patch...
  below threshold". A threshold is exactly the "tuned by nothing" number the project
  forbids, and PLAN.md's wording predates that lesson. The verdict honors PLAN.md's
  intent (stop bad work before the expensive stretch) without inventing a cutoff,
  and it says which criterion failed and why, which a scalar hides. Noted in PLAN.md.
- **Advisory before gate.** Stage 1 prints; it does not gate. Same one-shot-before-
  loop discipline as Layer 2 before Layer 3 and the reviewer before the fixer.

## Staging

- **Stage 1: the judge, standalone, advisory (built).** Grade the loop's output,
  structured three-state verdict, `--judge`, printed not acted on.
- **Stage 2: the gate.** Run the judge before the review/fix stretch; a BLOCK stops
  the run, or feeds the judge's reasons back to the builder as a retry, so the
  expensive stretch never runs on a wrong-but-passing patch.
- **Stage 3: the eval dataset.** A small set of labelled real patches (satisfied /
  not) to measure the judge's own accuracy, so the gate is not itself lying. The
  "who judges the judge" piece, and the P2 eval-dataset exercise.

## Stage 1 (built)

- `judge.py`: `CriterionOutcome` (satisfied/not_satisfied/uncertain) and
  `JudgeVerdict` (pass/block/unguarded), the semantic siblings of `checks.py`'s
  Outcome/Verdict. `CriterionVerdict` validates at construction. `JUDGE_RULES` is a
  stable cache-friendly prompt. `parse_verdict` is strict like `parse_edits`/
  `parse_findings`: a `--- verdict` block of JSON we parse ourselves, refusing prose,
  a non-list, an unknown outcome, an out-of-range index, a duplicate, or a verdict
  that does not cover every criterion exactly once (a partial judgment is not a
  judgment). `run_judge` grades the check-less criteria; a ticket with none is
  UNGUARDED and makes no model call. Cost records as `role="judge"` through the one
  door.
- `run.py`: `--judge`, opt-in, advisory. A `JudgeError` is reported, not raised.
- `tests/test_judge.py`: self-contained fake, 14 cases (pass/block/uncertain,
  unguarded-and-free, construction guard, strict-parse refusals, recorded as
  judge/eval, order).

Proven on real runs:
- Thin `truncate` that passes its test but does not reject a negative length ->
  BLOCK, the negative-length criterion `not_satisfied` with a correct reason, the
  other `satisfied`. $0.000591 (TASK-JUDGE-THIN).
- Robust `truncate` -> PASS, both satisfied (it cited the `raise ValueError`).
  $0.000573 (TASK-JUDGE-ROBUST).
- All-machine-checked ticket -> UNGUARDED, no model call, nothing spent.
- `ratio_by_role` now shows `judge` as its own cost line.

## Verification

- `.venv/Scripts/pytest.exe -q` (bare, CI-style) green, 191 tests.
- The decisive real check above: a patch the tests pass but a semantic criterion
  does not, caught by the judge with the criterion and the reason.
- CI green (real Postgres). No schema change this stage.
