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
- **Stage 2: the gate (built).** After tests pass, the judge grades the semantic
  criteria, and a BLOCK feeds its reasons back to the builder like a test failure,
  so the loop rebuilds until the judge is satisfied or the attempt budget is spent.
- **Stage 3: the eval dataset.** A small set of labelled real patches (satisfied /
  not) to measure the judge's own accuracy, so the gate is not itself lying. The
  "who judges the judge" piece, and the P2 eval-dataset exercise. Doubly needed now
  that Stage 2 lets the judge drive the builder.

## Stage 2 (built)

The judge becomes a gate inside the loop (`loop.py`), opt-in via `--gate`.

- **On BLOCK, loop back to the builder (the user's call, Option B).** A blocked
  patch is fed the judge's failed criteria as feedback and rebuilt, exactly like a
  test failure, until the judge passes or `max_attempts` is spent. The user chose
  this over the safer "stop and report" with the full downside in view. The
  guardrails that make it responsible: one attempt cap bounds the rebuilds (no
  infinite loop, no unbounded spend); it is opt-in; the judge's cost is folded into
  the run total so it cannot hide; and it **fails open**, a judge whose own reply is
  unusable passes the code with a note rather than blocking it, because a broken
  sensor must not stop the machine and the judge is not yet measured (Stage 3).
- **Another documented deviation from PLAN.md's order.** PLAN.md wants the judge to
  drive the builder, which this does, but the honest caveat is that we are handing
  an unmeasured judge command authority before Stage 3 measures it. The guardrails
  above bound the risk; Stage 3 is what removes it.
- `loop.py`: `gate` and `judge_model` in `LoopState`/`run_loop` (both default off /
  base model, so every existing caller is unchanged). `_gate` runs the judge after
  tests pass; BLOCK routes through `_decide_fail` with `_judge_feedback`; the
  accumulated `judges` make the total cost honest and the last verdict reportable.
- `run.py`: `--gate` (forces the loop path so it works at any `--max-attempts`, with
  a note when there is no retry budget). The judge model comes from `ModelMap`, so
  `models.json` can route the judge cheap too.
- Tests (in `test_loop.py`): block-then-pass, gate-off-never-judges, exhaust on a
  persistent block (bounded, not infinite), fail-open on a broken judge, and
  unguarded-and-free. Proven end to end on a real run: builder -> tests pass -> real
  judge PASS, cost including the judge (TASK-GATE). The block-then-rebuild cycle is
  proven deterministically by test; a real model kept writing correct code first
  try, so it was not watched live (same honesty as Stage 2's revert guard).

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
