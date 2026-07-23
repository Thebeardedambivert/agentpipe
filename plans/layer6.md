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
- **Stage 3: the eval dataset (built).** A small set of labelled patches to measure
  the judge's own accuracy, so the gate is not itself lying. The "who judges the
  judge" piece, and the P2 eval-dataset exercise. Doubly needed now that Stage 2
  lets the judge drive the builder.

## Stage 3 (built)

The judge gets graded. `evals.py` (dataset, scorer, CLI), `evalstore.py` (the audit
port), migration 004 (`judge_evals`, `judge_accuracy`, `judge_stability`), and a
dataset of eight labelled cases under `evals/cases/`.

**Decisions made with the user, and why.**

- **A case is a directory**, not a bespoke file format: `ticket.md` parsed by
  `Ticket.from_file`, `code/` read through the real `Repo`, `case.json` for the
  labels. Any custom format would be a second way to read a ticket, and a second
  implementation that drifts from the first is the `InMemoryCallStore` /
  `PostgresCallStore` bug in a new costume. The eval has to feed the judge exactly
  what production feeds it or it is grading a judge that does not exist.
- **Labels are two-state where the judge is three.** A labeller who is uncertain
  has not finished making the case, so `uncertain` is an answer the judge may give
  and never a ground truth it can be measured against. A consequence, taken
  deliberately: an "uncertain is the right answer" case is not expressible, so
  abstention is measured as a miss (a safer miss than a false pass) rather than
  scored as correct. Weakening the label rule to allow it would have cost more than
  the case was worth.
- **Labels carry the criterion's text, not just its index, and the loader refuses a
  mismatch.** Without it, reordering a ticket's acceptance bullets silently
  repoints every label and the eval reports a confident wrong number. That is the
  shape of all five bugs in STATE.md, and the guard is three lines.
- **Real and constructed cases, never merged in a number.** Harvesting only real
  runs is survivorship-biased in the exact direction that matters: the dangerous
  case is the judge calling wrong code satisfied, and by definition that is a case
  nobody noticed, so it never appears in a harvest of runs we were happy with.
  Constructed cases aim at that quadrant directly. The cost is that they were
  written by the same person who wrote the judge's prompt, which the split cut
  keeps visible.
- **Counts, never rates**, in the report and in the view. At eight cases one
  flipped verdict moves a percentage by double digits and still reads as a
  measurement. `graded` sits next to every count so anyone can divide while looking
  at what they are dividing. What would settle it: enough cases that a single flip
  cannot move the number by more than a point.
- **`rules_hash` on every row.** Rows from before and after a `JUDGE_RULES` edit
  are not comparable, and averaging them produces a number describing no judge that
  ever existed.
- **`--repeat N`, default 1.** Each sample runs at a distinct `attempt_index`,
  which is in the idempotency key, so samples are genuinely new paid calls rather
  than replays of one answer.

**What the first real run said, stated plainly.**

`gpt-5.4-mini`, 8 cases, 16 criteria, $0.005021: **16 of 16 agreed, zero false
passes, zero false blocks, zero abstentions, 8 of 8 verdicts right for the right
reason.** At `--repeat 5`: 80 of 80, and `judge_stability` reports not one criterion
where the judge gave two different answers.

**Do not read that as a win.** The plan for this stage said in advance that a
perfect first result is evidence the constructed cases are too easy, not evidence
the judge is good, and that is how it was recorded. The judge caught the
false-pass bait (a range check that validates correctly and then returns a default
anyway) and the false-block bait (correct code whose only error path is an implicit
`KeyError`), which is genuinely more than a pattern-matcher would manage. But an
instrument that has never disagreed with its calibration has not been calibrated.

## Stage 3b: making the dataset bite

The stage shipped with "acquire a case the judge gets wrong" as its open item.
This is that work.

**Two cheap checks first, before writing anything.**

- [JudgeBench](https://arxiv.org/pdf/2410.12784) (ICLR 2025) is the standing
  benchmark for LLM judges. The best model on it scores **64%**. Scoring 100% far
  above the field's best is a statement about the exam, not the student. This
  turned "the dataset is probably too easy" from an opinion into an outside number.
- Re-running the eight cases with `gpt-5.4-nano` as judge, for **$0.001408**, also
  gave **16 of 16**. A dataset that cannot separate a model from one 3.7x cheaper
  cannot answer the routing question it was built to answer. Recorded carefully:
  "nano judges as well as mini" is *not* supported. Only "the dataset could not
  tell" is.

**Six new cases, in matched pairs.** Three failure modes, each with a buggy and a
correct implementation of the *same ticket*. A pair is a controlled test: a judge
that answers both halves identically has not read either, and no single case can
reveal that. Sources are documented outside this project rather than invented here:

| pair | failure mode | external source |
|---|---|---|
| `strip-suffix-*` | `str.strip(".com")` trims characters from both ends, not a suffix | PEP 616 added `removesuffix` citing "repeated issues... user confusion" |
| `sorted-tie-order-*` | `reversed(sorted(...))` also reverses tied runs | why `reverse=True` exists |
| `float-money-*` | `==` on floats for money | the same rule CLAUDE.md sets for cost |

**Every claim in every label was verified by executing it.** Two candidate claims
sourced from memory were discarded at that step because running them proved them
false (`sum([0.1]*10) == 1.0` is `True`; a chosen `strip` example returned the
correct answer by luck and did not demonstrate the bug). Both would have shipped as
ground truth. This is the lesson of the whole stage in miniature: the dataset is
the answer key, and an answer key nobody checks is a confidently wrong dashboard.

**The finding: a false pass, the dangerous quadrant.** For
`return sum(prices) == expected` against the criterion "amounts that are
mathematically equal are reported as matching", `gpt-5.4-mini` answers
**satisfied**, in **6 of 6 samples**, `judge_stability` reporting
`distinct_answers = 1`. Its five recorded reasons are rewordings of one move:

> "The function returns True when sum(prices) equals expected, covering
> mathematically equal amounts."

**It restates the code and treats the restatement as proof.** It never asks whether
floating-point `==` means "mathematically equal" (`sum([0.1, 0.2]) == 0.3` is
`False`). Under `--gate` this patch reaches the working tree every time, and it is
a money bug in a checkout path. `gpt-5.4-nano` has the identical blind spot, which
points at `JUDGE_RULES` rather than at model capability.

**The dataset now discriminates, and only on the reasoning metric.** Across the six
new cases mini disagrees once; nano disagrees four times (same false pass, plus a
false block on the *correct* stable sort, plus two abstentions). Verdict counts
read 13 of 14 for both and would call them equivalent. Right-verdict-for-the-
right-reason separates them **13 to 11**. That metric was argued for on principle
when it was written; this is it paying rent.

## Stage 3c: the obvious prompt fix, and why it was reverted

With a blind spot identified, the obvious next move is to fix the prompt. It was
tried, measured, and put back. **The result is negative and is recorded because it
is negative**, which is the only kind of result that is easy to lose.

**The change.** A general instruction, deliberately not one about floats, because a
prompt patched at a known case is overfitting wearing a fix's clothes:

> Decide from what the code does, not from what it looks like. Pick a concrete
> input that the criterion is about, follow the code with that input, and see what
> actually comes out. [...] Your reason must say what happens for a specific input.
> Restating what the code says is not a reason.

**What happened.** `rules_hash` moved `32fd708260d5c68f` -> `5d671398b831c49c`, so
the before and after rows are separable in `judge_evals` rather than averaged.

| | before | after |
|---|---|---|
| false pass | 1 | 1 |
| false block | 0 | 0 |
| right verdict | 13/14 | 13/14 |
| right verdict for the right reason | 13/14 | 13/14 |
| cost, same 14 cases at full price | $0.008853 | $0.011866 |

No accuracy change. About 34% more expensive per gated run, forever.

**But the failure moved, and that is the finding.** The reason field changed from

> "The function returns True when sum(prices) equals expected, covering
> mathematically equal amounts."

to

> "For prices=[1, 2] and expected=3, sum(prices) is 3 so the function returns True."

The judge obeyed the instruction exactly. It picked a concrete input and traced the
code. Then it picked `[1, 2]` and `3`, integers, which have no floating point
problem at all, and concluded the criterion was met. **It complied with the letter
of the instruction and still got the wrong answer, by choosing an input that does
not exercise the thing being asked about.**

So the blind spot is not "the judge does not check". It is "the judge chooses a
friendly input". That is a different bug, and a sharper one: the next instruction
would have to be about *which* input to pick, not whether to pick one.

**Why it was reverted rather than iterated.** Steering the next attempt with the
answer in hand is how a prompt gets tuned to fourteen cases and nobody remembers
what it cost. Reverting is free (every answer under the old hash is cached, so
re-verification replays at $0 and reproduced the baseline exactly), both attempts
stay in the table under their own `rules_hash`, and the next attempt can start from
a recorded negative rather than a vague memory that "we tried something".

Two smaller things the experiment established, worth keeping:

- The trade did not bite. Making the judge more suspicious did not produce a single
  false block, which was the predicted risk. The six PASS cases in the dataset are
  what make that a measurement rather than a hope.
- Reading only the score would have said "no change, try again". Reading the reason
  said the failure had moved. **A verdict-only eval would have thrown away the most
  useful thing this experiment produced.**

**Left deliberately undone.** `JUDGE_RULES` is back to the shipped version, so the
gate's behaviour is unchanged by this stage. The blind spot is open and recorded in
STATE.md.

**A second, found by self-review before it could bite.** `run_judge` raised
`JudgeError` after the call had been made and billed, and the exception carried no
record, so an unusable reply was reported as a free sample. That makes the most
expensive failure the harness can have (spends money, returns nothing, and in
production silently disables the gate) look like the cheapest. `JudgeError` now
carries its `CallRecord`, and the harness counts the cost.

**A bug the stage found in itself.** The first `--repeat 5` run reported $0.024944.
Only $0.019923 was billed: sample 0 of every case replayed from the previous run,
and `cost_usd` on a replayed record carries the original price. The report now
separates billed from full price. The money is the small half; the real half is
that a replayed sample is not an independent draw, so counting replays toward
stability would report perfect consistency for a judge that was asked once. Pinned
by `test_a_replayed_sample_is_not_counted_as_spend`, which names the run.

**Known gaps, deliberate.**

- Eight cases, five of them constructed, is a smoke test for a sensor, not a
  measurement of accuracy.
- `TASK-GATE` could not be harvested: the run's files were never captured. Real
  cases need a harvest path, which does not exist yet.
- Packs are ~355 tokens each, well under the ~1,024 threshold, so no eval call
  earns the cached-input discount. Expected, and consistent with the baseline.
- Nothing about the gate changed this stage. Its fail-open behaviour, the absence
  of a threshold, and CI not gating on judge accuracy are all unchanged, because
  this stage measures and does not tune.

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

- `.venv/Scripts/pytest.exe -q` (bare, CI-style) green, 237 tests after Stage 3
  (191 at Stage 1, 196 at Stage 2).
- Stage 1's decisive real check: a patch the tests pass but a semantic criterion
  does not, caught by the judge with the criterion and the reason.
- Stage 3's: the numbers above, read back out of `judge_accuracy` and
  `judge_stability` rather than off the terminal.
- `python -m agentpipe.evals --dry-run` validates the whole dataset, including
  label-to-ticket agreement, for nothing.
- CI green. Stage 3 adds migration 004, which CI applies to a fresh database on
  every push; the eval store contract tests run against real Postgres there.
