# STATE

Where this actually is, as of 23 July 2026. Read after CLAUDE.md and PLAN.md.

CLAUDE.md is the rules. PLAN.md is the design. This is the situation.

## Built

Layers 0, 1, 2, 3, Layer 5 complete (reviewer, fixer loop, audit table), and Layer 6
complete (the judge, the judge as a gate, and the eval dataset that measures it).
237 tests, all passing under the bare `pytest` CI runs. CI
(`.github/workflows/ci.yml`) runs the suite against a real Postgres on every push.

```
telemetry.py   the seam. Every model call goes through MeteredClient.call()
ticket.py      the contract. Refuses vague tickets before anything is billed
checks.py      the staleness gate and the validation runner. Exit-code contract
repo.py        git ls-files, plus which files are worth paying for
pack.py        assembly, ordered by volatility so the cache prefix stays stable
patch.py       parses the model's reply, refuses ambiguity, writes to disk
builder.py     the wiring for one attempt. Ticket in, files out
loop.py        the LangGraph loop: build, validate, retry, resume. Layer 3
review.py      the reviewer: passing code in, ranked structured findings out
fixer.py       the fixer loop: fix the worst finding, revalidate, revert if broken
config.py      ModelMap: which model each role uses, defaults to the base model
findings.py    the audit port: record findings and outcomes to review_findings
judge.py       the eval gate: grade check-less acceptance criteria, three-state
loop.py (gate) --gate makes the judge a second gate in the loop, blocks rebuild
evals.py       the dataset that grades the judge. Labelled cases in, matrix out
evalstore.py   the accuracy port: record what the judge said vs what was labelled
run.py         CLI. --max-attempts loops, --review, --fix, --judge, --gate
preflight.py   four checks before you trust any number
```

## The baseline

One real call, on TASK-1, with `gpt-5.4-mini`:

```
in 1,733   out 190   thinking 0   ratio 9.1   $0.0022   cache 0%
```

Andrew's is 700. Do not read 9.1 as a win. It is one call, on a 10-file repo,
with no loop, on a ticket whose work was already done. It is the first reading
from a working instrument, not a measurement.

**Two things that row is not telling you:**

`cache_hit_pct` is 0 on this row, but Layer 1's argument is no longer unproven.
A controlled two-call probe (identical prefix, different suffix, `gpt-5.4-mini`,
back to back) showed it fire:

```
call 1 (cold)  input 3,604   cached 0
call 2 (warm)  input 3,607   cached 3,328   cache_hit 92%
```

So stable-content-first genuinely earns the cached-input discount: 92% of the
repeated prefix billed at the cached rate. **The catch, and it is the real
lesson:** the prefix had to be padded to ~3,600 tokens to see it, because the
provider only caches prompts above ~1,024 tokens. A real ticket's RULES block is
a few hundred tokens, so on a small repo a loop's attempt 2 still reads 0%: the
design is correct, but the discount only engages once packs are large. The
70k-to-100 thesis's biggest lever works, and now we know the exact condition
under which it turns on.

`avg_thinking` is 0. This model spends no reasoning tokens here, so the output
budget in `builder.py` solves a problem that has not occurred. Still correct to
have. Was not the fix it was sold as.

## Next, in order

**1. Proven end-to-end on real work. (Done, 18 July 2026.)**
The pipeline ran a real ticket start to finish on `gpt-5.4-mini`: TASK-TRUNCATE,
implement `truncate(text, length)` on a scratch repo. Result: PASSED in 1 attempt,
the agent wrote correct code (it even added a `max(0, ...)` guard the spec did not
ask for), validation gated it, independently re-confirmed. Real numbers: cost
$0.000482, pack 384 tokens in / 43 out, a real trace (`13c1e7...`, dur 4445ms).
Cache read 0%, as expected: 384 tokens is well under the ~1,024 threshold. Kept in
`model_calls` as the second real datapoint after TASK-1. The retry path was not
triggered live (the model got it first try); it stays proven by tests. Still open:
a bigger, realistic ticket, which is where the cache would fire and a live retry
could actually be watched.

**2. The stale ticket hole. (Partially closed.)**
Nothing used to ask "is this already true?", so the agent always did something.
Now `checks.py` runs a ticket's acceptance checks before any model call: a ticket
whose checks all pass is stale and the run stops, having spent nothing. An
acceptance bullet carries its own check inline, so the two cannot drift. The gate
has three states, not two: pass, not-done, and broken, because a check that
cannot run is a different fact from work that is not done, and conflating them is
this project's signature bug.

Still open, and deliberately so:
- Unguarded by default. A ticket with no checks still proceeds; the gate says so
  out loud rather than pretending. Optional was chosen because we have no data yet
  on what fraction of real tickets can express a checkable acceptance.
- Structural only. "Does the file read well?" is semantic and waits for Layer 6's
  judge. A weak check that passes for the wrong reason is worse than none.
- A Windows shell seam: a command that does not exist exits 1 (same as not-done)
  on cmd.exe but 127 (-> broken) on POSIX. Caught in CI, missed locally. It never
  reads as "done", so the failure is safe, but it is real. Documented in checks.py.
- Trust boundary: checks run with your privileges. Fine while you author your own
  tickets, needs a sandbox the day they come from anywhere you do not control.

**3. Layer 5 and Layer 6 are both complete. Next is Layer 4 or Layer 7, the two
industrial layers, and neither is urgent.** The loop, crash-safe resume, and the
tracing tree are done; the cache claim is proven (92%, above the ~1,024-token
threshold; see the baseline).

Layer 5 is staged (see `plans/layer5.md`): Stage 1 the reviewer, Stage 2 the
fixer loop plus model routing, Stage 3 the audit table.

Stage 1 (reviewer) reads passing code and returns ranked, structured findings
(`--- findings` block of JSON, parsed strictly by us, refused when malformed).
Opt-in (`--review`), advisory. Proven on a real run against thin `truncate` code
for $0.000737, recorded as `role='reviewer'` (TASK-REVIEW-DEMO).

Stage 2 (fixer loop, on the `stage2-fixer` branch) repairs findings one at a time,
worst first: review, fix the worst finding, re-validate, keep if green, revert if
broken (an in-memory snapshot, not git; not crash-safe, which is Layer 7's job).
`ModelMap`/`models.json` route each role to a model, defaulting to the base model.
Opt-in (`--fix`). Proven on real runs: with the fixer on `gpt-5.4-mini` it flagged
and fixed the ellipsis bug (`text[:length] + "..."` -> `text[:length]`), validation
stayed green, outcome `fixed`, $0.000310 for the fix, recorded as `role='fixer'`
(TASK-FIX-MINI2). A real datapoint worth keeping: with the fixer routed to the
cheaper `gpt-5.4-nano`, the fix was correct code but the model twice omitted the
`--- end` terminator, so the strict parser refused it and marked it `unfixable`,
leaving the code untouched (TASK-FIX-DEMO, TASK-FIX-MINI). The cheapest model is
not automatically the right fixer: routing trades cost against format reliability.
The revert guard is proven byte-for-byte by test, not yet watched live (a real
model rarely writes a breaking fix on demand).

Stage 3 (audit table, `review_findings`) records both the fix loop's outcomes and
advisory `--review` findings. `findings.py` is a `FindingStore` port (InMemory +
Postgres, contract-tested like `CallStore`); recording swallows its own errors so
the audit can never fail a run. Two views: `finding_outcomes` (severity x outcome)
and `fixer_reliability` (fixed/reverted/unfixable per fixer model). Proven on real
runs: nano `unfixable`, mini `fixed`, one advisory `reported` (TASK-S3-NANO/MINI/
REV), and `fixer_reliability` then read mini 100% / nano 0% fix rate straight from
the table. That is the routing question answered from data instead of anecdote, the
whole point of the layer. The cost-vs-reliability line for the fixer model is now
measurable; it is not yet a large enough sample to set a default from.

Layer 6 Stage 1 (judge, on the `layer6-judge` branch; see `plans/layer6.md`) grades
the ticket's check-less acceptance criteria, the semantic ones no exit code can
verify. Per-criterion three-state verdict (satisfied / not_satisfied / uncertain),
PASS only if all satisfied, a deliberate documented deviation from PLAN.md's "score
below threshold" (a threshold is a tuned-by-nothing number; PLAN.md now says so).
Opt-in (`--judge`), advisory this stage. A ticket with no such criteria is UNGUARDED
and makes no model call. Proven on real runs: thin `truncate` passes its test but
the judge returned BLOCK, catching that a negative length is not rejected
(TASK-JUDGE-THIN, $0.000591); robust code PASSED (TASK-JUDGE-ROBUST); an
all-machine-checked ticket was UNGUARDED with no call. Cost lands under `role=judge`
in `ratio_by_role`.

Layer 6 Stage 2 (the gate, on the `layer6-gate` branch) wires the judge into the
loop, opt-in via `--gate`. After tests pass the judge grades the semantic criteria,
and a BLOCK feeds its reasons back to the builder like a test failure, so the loop
rebuilds until the judge passes or `max_attempts` is spent. This is the user's call
(Option B, judge drives the builder) chosen over "stop and report" with the full
downside in view. Guardrails, so responsible-B not naive-B: one attempt cap bounds
the rebuilds (no infinite loop, no unbounded spend); opt-in; the judge cost is
folded into the run total so it cannot hide; and it **fails open**, a judge whose
own reply is unusable passes the code with a note rather than blocking, because a
broken sensor must not stop the machine and the judge is not yet measured. A second
documented deviation from PLAN.md's order: it hands an unmeasured judge command
authority, which Stage 3 is what makes safe. Proven end to end on a real run:
builder -> tests pass -> real judge PASS, total cost including the judge (TASK-GATE).
The block-then-rebuild cycle is proven deterministically by test; a real model kept
writing correct code first try, so it was not watched live (same honesty as Stage
2's revert guard).

Layer 6 Stage 3 (the eval dataset, on the `layer6-evals` branch) is what measures
the judge that Stage 2 put in charge. Eight labelled cases under `evals/cases/`,
each a directory holding a real ticket (parsed by `Ticket.from_file`, no second
parser), the code as it was (read through the real `Repo`), and labels saying which
criteria that code actually meets. `evals.py` grades, `evalstore.py` records, and
migration 004 adds `judge_evals` with `judge_accuracy` and `judge_stability`.

The two failures it exists to see, neither of which anything caught before: **false
pass** (labelled not_satisfied, judged satisfied: the gate waves wrong code
through) and **false block** (labelled satisfied, judged not_satisfied: the gate
burns a rebuild attempt on correct code). Before Stage 2 only the first mattered
and it was advisory. After Stage 2 both cost money every run.

Labels are two-state where the judge is three, on purpose: a labeller who is
uncertain has not finished making the case. Each label carries its criterion's
*text* as well as its index, and the loader refuses to run when they disagree,
because reordering a ticket's bullets would otherwise silently repoint every label
and produce a confident wrong number. Real and constructed cases are reported as
separate cuts and never merged: harvesting only real runs is survivorship-biased in
exactly the direction that matters, since the dangerous case is one nobody noticed.

**The first real result, and how to read it.** `gpt-5.4-mini`, 8 cases, 16
criteria, $0.005021: 16 of 16 agreed, zero false passes, zero false blocks, zero
abstentions, 8 of 8 verdicts right for the right reason. At `--repeat 5`: 80 of 80,
and `judge_stability` shows not one criterion where the judge gave two different
answers.

**That was not a win, and the plan said so before the run.** A perfect first result
is evidence the constructed cases are too easy, not evidence the judge is good. Two
cheap checks then confirmed it:

- **External.** [JudgeBench](https://arxiv.org/pdf/2410.12784) (ICLR 2025) is the
  standing benchmark for LLM judges and the best model on it scores **64%**.
  Scoring 100%, far above the field's best, is a statement about the exam.
- **Internal, for $0.001408.** Re-running with `gpt-5.4-nano` as judge also scored
  **16 of 16**. A dataset that cannot separate a model from one 3.7x cheaper cannot
  answer the routing question it was built for. The trap to avoid: "nano judges as
  well as mini" is not a supported conclusion. The only supported conclusion is
  that the dataset could not tell them apart.

## The dataset was expanded to 14, and the judge failed

Six new cases: three failure modes, each paired **buggy and fixed against one
shared ticket**, so every pair is a controlled test of whether the judge can tell
the two apart. The failure modes are documented outside this project rather than
invented here (PEP 616 exists *because* programmers keep misusing `str.strip` as
suffix removal; sort stability; binary floating point on money), and every claim in
every label was verified by executing it. Two candidate claims were discarded at
that step because running them showed they were false, which is the whole argument
for verifying rather than recalling.

**The finding is a false pass, the dangerous quadrant.** Given
`sum(prices) == expected` and the criterion "amounts that are mathematically equal
are reported as matching", `gpt-5.4-mini` answers **satisfied**, in **6 of 6
samples** (`judge_stability`: `distinct_answers = 1`). Its reasons are five
rewordings of one move:

> "The function returns True when sum(prices) equals expected, covering
> mathematically equal amounts."

It restates the code and treats the restatement as proof. It never asks whether
floating-point `==` means "mathematically equal", which it does not:
`sum([0.1, 0.2]) == 0.3` is `False`. With `--gate` on, that patch reaches the
working tree every time, and it is a money bug in a checkout reconciliation.
`gpt-5.4-nano` shares the identical blind spot, which points at the prompt rather
than the model.

**And the dataset now discriminates.** On the six new cases mini disagrees on one
criterion; nano disagrees on four (the same false pass, plus a false block on the
*correct* stable sort, plus two abstentions). Verdict counts alone read 13 of 14
for both and would call them equivalent. **Right-verdict-for-the-right-reason
separates them, 13 to 11.** That is that metric earning its place: the judge's
named criteria become the builder's instructions, so a right answer reached by
wrong reasoning sends the builder to fix the wrong thing.

Report and view print counts, never rates: at fourteen cases one flipped verdict
still moves a percentage by several points and reads as a measurement. Nothing here
is a pass mark, no threshold was introduced, and the gate's fail-open behaviour is
unchanged. This stage measures; it does not tune.

Layer 4 (event-sourced replay) and Layer 7 (Temporal) are the industrial layers:
worth it at volume or for the learning, not before. `checks.py` already seeded the
validation runner: the same check run before is the staleness gate, run after is the
success check.

## Decisions already made, so nobody relitigates them

**LangGraph is deferred to Layer 3.** PLAN.md originally put a single-node graph
at Layer 2. A graph with one node and no edges is ceremony: it teaches the
imports and hides the point. The conditional edge at Layer 3 is the point.

**The file selector is deliberately stupid.** Ticket hints win outright, fallback
is word overlap weighted by rarity. No embeddings, no similarity index, no model
call to pick files. Not laziness: there is no evidence yet about what good
selection looks like, and anything cleverer would cost tokens to make a decision
about saving tokens. When the table says the ranking picks badly, there will be a
reason and a number.

**A score floor was tried and removed.** The number would have been invented, and
on a small repo it silently selected nothing. See CLAUDE.md.

**gpt-5.4-mini, knowingly.** `gpt-5.4-nano` is ~3.7x cheaper ($0.20/$1.25 vs
$0.75/$4.50 per 1M). Mini was chosen anyway, which is fine, but it is the pricier
option and not the cheaper one. `gpt-4o-mini` is not on the current pricing page.

**OpenRouter/GLM is a config change, not a code change.** `MeteredClient` takes
the client as a constructor argument for exactly this. Two things need fixing
when you switch: `_extract_usage` reads OpenAI's `prompt_tokens_details` shape,
and the span hardcodes `gen_ai.system = "openai"`, which would be a lie.

## Known gaps

Both of the Layer 0 gaps recorded in PLAN.md are now closed.

**The judge has a known, stable blind spot. The obvious fix was tried and did not
work. (Open.)** It reads an equality check and concludes the criterion about
equality is met, without asking whether that check is correct. 6 of 6 samples on
`gpt-5.4-mini`, and `gpt-5.4-nano` does the same.

A general prompt instruction was added telling the judge to trace a concrete input
rather than reason from the shape of the code, and to give a reason naming that
input. Measured, then reverted. `rules_hash` moved `32fd7082` -> `5d671398`, so both
attempts sit in `judge_evals` separately rather than averaged:

- **No accuracy change.** Still 1 false pass, still 0 false blocks, still 13/14 on
  both verdict metrics.
- **About 34% more expensive** on the same fourteen cases ($0.008853 -> $0.011866
  at full price), for every gated run, forever.
- **The failure moved, which is the actual finding.** The reason changed from
  restating the code to `"For prices=[1, 2] and expected=3, sum(prices) is 3 so the
  function returns True."` The judge obeyed the instruction, traced a concrete
  input, and picked integers, which have no floating point problem. The blind spot
  is not "does not check", it is "chooses a friendly input". Different bug, sharper
  diagnosis, and the next attempt has to constrain *which* input.
- Reading the score alone would have said "no change, try again". Reading the
  reason said the failure had moved. A verdict-only eval would have discarded the
  most useful output of the experiment.

Reverted rather than iterated on purpose: steering attempt two with the answer in
hand is how a prompt ends up tuned to fourteen cases with no record of the cost.
Reverting was free, since every answer under the old hash is cached; re-running
reproduced the baseline exactly at $0 billed.

**The blind spot is narrower than it first looked, and the shape of it matters.**
The dataset was pushed to 20 cases to test whether the float failure was one member
of a family ("the judge accepts a nearly-right operation"). Three siblings, each a
matched buggy/fixed pair: floor division losing a penny, `==` on email missing
capitalisation, `sorted()` ordering by character code. **The judge got all six
right, stably. The hypothesis is disconfirmed, for $0.0086.**

The real pattern is not the kind of operation, it is whether the defect exists in
the text. `[total // people] * people` visibly discards a remainder; `a == b`
visibly lacks normalisation; `sorted(names)` visibly lacks a key. But
`sum(prices) == expected` **is correct code** for integers, `Decimal` and
`Fraction`. It is wrong only because of how binary floating point stores decimals,
and that fact is nowhere in the source. **The judge fails when catching the defect
requires knowing runtime behaviour rather than reading the code.**

That also explains the failed prompt fix: told to trace a concrete input it chose
`1, 2, 3`, which read as laziness but was not. For those values the code genuinely
is correct. It answered the only question reading can ask. Further prompt attempts
should therefore be expected to fail for a principled reason, which is worth
knowing before paying for attempts two through four, and it is the argument for
letting the judge *run* code rather than reason about it.

**Both cheap screening methods were measured, and both have the same limit.**
Cheap-model disagreement flagged four cases; in all four mini was right and nano
wrong, so it is a model-selection signal, and it missed the one case mini fails
because both models agree there. Stability found nothing unstable, yet
`float-money-buggy` is perfectly stable and wrong (6 of 6) while the new cases are
perfectly stable and right (3 of 3). **Both measure confidence, neither measures
correctness**, and a confidently wrong judge is the dangerous case. Useful for
comparing models; useless for finding blind spots.

**The eval dataset has no harvest path. (Open, and it is the one that matters.)**
Cases are built by hand. `TASK-GATE` could not become a case at all because the
run's files were never captured, so a real judge verdict from a real run is already
unrecoverable. Eleven of fourteen cases are `constructed`, and the provenance field
has only two values, so a case whose failure mode is documented in the wild (PEP
616, sort stability, float money) is recorded the same as one invented here. That
understates the newer cases rather than overstating them, which is the safe
direction, but the distinction is real and the field cannot express it. Until a
real disagreement can be turned into a case cheaply, the dataset grows in the
direction of what we imagine rather than what happens. What would fix it: capture
the judged files alongside the verdict, so `--gate` on a real ticket leaves behind
everything a case needs.

**Spans go nowhere. (Closed.)** `trace_id` and `span_id` used to write as all
zeros, because no tracer was configured and OTel's no-op default discarded every
span. Closed at Layer 3: `configure_tracing()` sets a real TracerProvider
(opt-in, from the CLI, so tests stay no-op), and `run_loop` wraps a run in a
parent span, so a run's calls now share a real trace_id and read as a tree.
Shipping spans to a real backend is one processor away, once there is a viewer.

**Contract tests skip without a DSN. (Closed.)** `tests/test_store_contract.py`
only runs against Postgres when `AGENTPIPE_DSN` is set, so a bare CI job would
have skipped the tests that catch store divergence: a promise nobody checks.
Closed by `.github/workflows/ci.yml`, which attaches a real Postgres 16, loads
the schema and migrations, and sets the DSN, so those tests run on every push.
The first run also caught a latent bug: migration 001 inserts columns into the
middle of the `ratio_by_role` view, which `create or replace view` refuses on a
fresh database. It never showed on Supabase, which was built by hand. CI was the
first clean-room replay of the schema, start to finish.

## Environment

**Supabase project `agentpipe`**, free tier. **Pauses after 7 days idle**, and a
paused project fails silently through `_safe_record`. Run `python -m
agentpipe.preflight` before trusting any number after a quiet week.

**`prices.json` and `.env` are gitignored** and exist only on the dev machine. A
fresh clone needs both. `PriceMap.from_env()` raises rather than defaulting, on
purpose: a confidently wrong cost dashboard is worse than none.

**Windows, PowerShell, Python 3.14.** Traps hit already, all now handled but worth
knowing:

- `Set-Content -Encoding utf8` writes a BOM, which breaks `tomllib`. Use `ascii`
  for config files.
- The repo folder was created from an admin shell, so git needed
  `git config --global --add safe.directory 'C:/Users/Cyril Uzochukwu/code/agentpipe'`.
- `.gitattributes` normalises to LF. This is load-bearing: `patch.py` writes with
  `newline="\n"` so an agent's two-line change does not appear as a whole-file
  diff.
- `pytest` takes ~90s here vs ~2s on Linux, because the tests `git init` real
  repos and Defender scans each one. Excluding the `code` folder from real-time
  scanning fixes it.

## Attribution

The architecture is Andrew Onwe's (@DrewCodesIt). The two rules the design rests
on, the workflow owning the loop and rebuilding context rather than accumulating
it, are his, as is the harness split in Layer 5 and the 70,000-to-100 measurement
that started this. Credit sections in README.md and PLAN.md. Do not remove them.

**Open:** Andrew has not been asked whether he wants his name on a public repo.
That conversation should happen.

## The shape of every bug so far

Five real bugs, all the same one: **the system reported success while quietly not
doing the thing.**

- pytest reported 95 passing from a test file in the wrong folder
- the in-memory store kept content, the Postgres one dropped it, and fifteen
  tests passed against the double
- a billed call returning nothing was recorded as 'ok' and became a permanent
  cache hit
- the contract tests wrote fixture rows into the live table and ratio_by_role
  averaged them in
- the idempotency key excluded the model, so routing the fixer nano->mini and
  re-running an identical pack replayed nano's answer instead of calling mini, and
  reported it as mini's (Stage 2 made it reachable; fixed by putting model in the
  key). A wrong model comparison that would have looked right.

None errored. All lied. This is why the meter exists, and it is why Andrew's
70,000 sat there unnoticed: nothing was broken, it just cost money.

When something looks fine, that is not evidence. Check the table.

**A sixth, running the other way (23 July 2026).** The eval harness's first
`--repeat 5` reported $0.024944 spent. Only $0.019923 was billed: sample 0 of every
case replayed from the run before it, and `cost_usd` on a replayed record carries
the original price. This one *over*stated rather than hid, which makes it the
friendlier direction, but it is the same family: a number that looked right,
computed correctly, and answered a different question than the one being asked.
The money was the small half. The real half is that a replayed sample is not an
independent draw, so counting replays toward stability would have reported perfect
consistency for a judge that was asked once. Fixed by separating billed from full
price; pinned by `test_a_replayed_sample_is_not_counted_as_spend`.
