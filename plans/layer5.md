# Layer 5: reviewer and fixer

Design plan, agreed in conversation. Read after PLAN.md (the why) and STATE.md
(the situation).

## Context

The loop stops when tests pass, which proves code *runs*, not that it is *good*.
Layer 5 adds a second opinion: a reviewer reads the passing code and returns
ranked, structured findings; a fixer repairs the ones worth acting on; validation
gates every fix so the cycle can only improve or leave things unchanged. It also
introduces model routing (a cheap model for narrow fixes), which PLAN.md calls
"the biggest single cost lever". Roles `reviewer`/`fixer` and attempt kinds
`review`/`review_fix` already exist in the seam's types (telemetry.py), so the
earlier layers left the door open for this.

The layer is also its own honest test: it records findings, severities, and what
happened to each, so we can answer "is the reviewer worth its cost?" from the
table, not from hope.

## Decisions locked (with the user)

- **Fork 1:** review runs only *after* validation passes. Never critique code that
  does not run.
- **Fork 2:** findings are a JSON array, parsed strictly and by us, not by a
  provider's json_schema feature. Delivered inside a `--- findings` / `--- end`
  delimiter block, the same delimiter shape `patch.py` already uses, so prose
  around the JSON is refused exactly the way `parse_edits` refuses prose. This
  keeps the OpenRouter/GLM switch a config change, not a code change (STATE.md).
- **Fork 3:** model routing is a config file `models.json` (loaded from
  `AGENTPIPE_MODELS`, like `prices.json`) with sensible defaults, so it is optional
  and friendly for other users, not a code change. Arrives with the fixer (Stage 2),
  where a cheap model actually earns its keep.
- **Fork 4:** after a fix, always re-validate; re-review up to a hard round cap.
- **Fork 5:** a fix that breaks validation is reverted with an *in-memory content
  snapshot*, not git. Before the fixer runs, the allowed files' current contents
  are read into memory and the set of pre-existing paths is noted; on a breaking
  fix, the captured contents are rewritten and any newly-created file is deleted.
  No git-state assumption, testable without subprocesses, matches how patch.py
  already writes whole files. Its one limitation is written into the code: the
  snapshot lives in memory, so it does not survive a crash mid-fix. That is
  deliberate, because crash-durability is Layer 7's job, not this stage's, and a
  half-built durability story in the wrong layer is worse than an honest gap.
- **Fork 6:** one finding per round, worst first, not a batch. Each round repairs
  the single most severe actionable finding, re-validates, keeps or reverts, then
  re-reviews from the real updated code. Isolates damage (a broken fix reverts only
  itself), lets the loop see findings that interact (fixing one can dissolve or
  reveal another), and gives Stage 3 clean per-finding outcomes. Costs more calls
  than a batch, bounded by the round cap and offset by routing the fixer cheap.
- **Review is opt-in** (`--review`), off by default, because it adds cost.
- **Plain loop, not LangGraph.** The control flow (review, filter, fix, revalidate,
  cap) is simple; a graph would be ceremony. Layer 3 already taught LangGraph; the
  conditional edge was the point there and is not here.

## Staging

Refined from the first draft of this plan. The original Stage 1 was the whole
review-fix loop. That bundles two new moving parts (a reviewer that must read true,
and a fixer loop that acts on its word) into one landing, which is the exact thing
the Layer 2 -> Layer 3 split exists to avoid: a loop hides bugs, so prove the
one-shot first. So:

- **Stage 1: the reviewer, one shot.** Produce ranked structured findings from
  passing code. No fixer, no loop, not wired into the retry graph. Provable on a
  real diff, the way the Layer 2 builder was provable before the Layer 3 loop.
- **Stage 2: the fixer loop, and model routing.** Consume findings, repair against
  *findings and current code only, never history*, re-validate, revert a fix that
  breaks tests, cap the rounds. Routing (`models.json`) lands here because a cheap
  fixer is what routing is for.
- **Stage 3: the audit table.** The measurement: record findings, severities, and
  outcomes so the six open questions get answered from the table, not hoped at.

Each stage lands on its own, branch -> CI -> PR -> merge, the same discipline as
Layer 3's stages.

## Stage 1: the reviewer (this stage)

A reviewer that reads the code the loop produced and returns ranked, structured
findings. It reads; it does not write. Findings are advisory this stage: printed
for a human, acted on by nobody. That is deliberate, so the sensor is proven to
read true before Stage 2 wires it to an actuator.

### New module `review.py`

- `Finding` (frozen dataclass): `severity`, `file`, `line` (optional), `issue`.
  `__post_init__` validates `severity` against a fixed set
  (`critical`/`high`/`medium`/`low`) and refuses an empty `file` or `issue`, so an
  invalid finding is unconstructible (the project's unrepresentable-invalid-state
  rule, the same one `CallRecord.__post_init__` enforces).

- `SEVERITY_ORDER`: the fixed severity set and its ranking, one source of truth for
  both validation and sorting.

- `REVIEW_RULES`: a stable system prompt (cache-friendly, never interpolated, like
  `pack.RULES`). It asks the model to return *only* a `--- findings` / `--- end`
  block whose body is a JSON array of `{severity, file, line, issue}` objects, and
  an empty array `[]` when there is nothing worth flagging. Empty array is a first
  class answer: "I looked and it is fine" must be sayable, or the model invents
  nitpicks to fill the silence.

- `parse_findings(reply) -> tuple[Finding, ...]`: extract the single
  `--- findings` block (reusing patch.py's anchored, non-greedy delimiter idea),
  `json.loads` its body, and build `Finding`s. Strict and unforgiving, mirroring
  `parse_edits`: prose instead of a block is refused, a body that is not a JSON
  list is refused, an object missing a field is refused, all with a clear message.
  An empty array parses to an empty tuple (clean), which is not an error.
  Malformed output raises `ReviewError`. Refusing costs one retry (Stage 2's loop);
  guessing costs a wrong review. This stage lets `ReviewError` propagate rather than
  retrying, because there is no loop yet to retry inside; the retry belt is Stage 2.

- `build_review_pack(ticket, repo, files)`: assemble the reviewer's context in the
  same most-stable-first order as `pack.build`, so the cached-input discount
  applies: `REVIEW_RULES` (system), then the ticket block (what was asked), then the
  current on-disk contents of `files` (what was produced). No feedback, no history.
  Determinism matters here for the same reason it does in `pack.build`, so this is a
  pure function of its inputs.

- `run_review(ticket, repo, client, model, files, min_severity="low") -> ReviewResult`:
  one call, no loop.
  1. `client.call(build_review_pack(...).as_list(), model=model, role="reviewer",
     attempt_kind="review", attempt_index=0, task_ref=ticket.ref)`. One `model_calls`
     row, so the reviewer's cost is recorded for free through the existing seam.
  2. `parse_findings(record.content)`.
  3. Rank by `SEVERITY_ORDER` and keep findings at or above `min_severity`.
  4. Return `ReviewResult`.

- `ReviewResult` (frozen): `findings` (ranked tuple), `record` (the one reviewer
  `CallRecord`), and derived `cost_usd` / `clean` (no findings). Plus
  `report_review(result)`: a human summary that lists findings worst-first with
  severity, file, line, and the cost of the review.

`min_severity` is a hypothesis, coded as a parameter with a docstring naming what
would settle it (per CLAUDE.md), not a constant: the audit table in Stage 3 is what
tells us where the useful/nitpick line actually sits.

### Model note

`run_review` takes `model` as a plain argument. The CLI passes the run's base model
this stage. `models.json` / `ModelMap` arrives in Stage 2 with the fixer, because
routing a cheap model at narrow work is what makes routing worth the config file,
and there is no narrow work yet.

### CLI (`run.py`)

Add `--review`, off by default. After `run_loop` returns `ok`, if `--review` is set,
call `run_review` on the files the loop actually wrote (the union of each
`BuildResult.written`), then print `report_review`. Findings are printed, not acted
on: the CLI says so, so nobody reads an advisory reviewer as a gate.

### Tests (`tests/test_review.py`)

A fake reviewer (a `SimpleNamespace` returning canned content, the same pattern as
the loop tests), so the suite stays free and deterministic and never calls a model.
Cases:

- clean code: reviewer returns `[]` -> `ReviewResult.clean` is true, no findings.
- findings present: parsed, and ranked worst-first (a `critical` sorts above a
  `low` regardless of the order the model listed them).
- `Finding.__post_init__` rejects an unknown severity and an empty issue.
- `min_severity` filters out findings below the threshold.
- malformed JSON (prose, a non-list body, a finding missing a field) -> `parse_findings`
  raises `ReviewError` with a clear message, and does not return a partial list.
- the reviewer call is recorded with `role="reviewer"`, `attempt_kind="review"`
  (asserted against the fake store), so Stage 3's audit has real rows to join to.

### Verification

- `pytest -q` green including `tests/test_review.py` (fakes, no spend).
- A real run: take a ticket, let the builder produce deliberately thin code (e.g.
  no input validation), then run with `--review` and watch the reviewer flag it,
  ranked, for real cost recorded in `model_calls` under `role='reviewer'`. Confirm
  an empty-array review on clean code reports "clean" and costs one recorded call.
- CI stays green (real Postgres). No schema change this stage, so nothing new for
  CI to exercise beyond the reviewer's recorded call.

## Stage 2: the fixer loop, and model routing (this stage)

Consume Stage 1's findings and act on them, safely. The safety spine: every fix is
re-validated, and a fix that breaks the tests is reverted, so the cycle can only
improve working code or leave it untouched, never degrade it.

### Model routing: `ModelMap` and `models.json`

`ModelMap`, in a new `config.py`, mirroring `PriceMap.from_env` but **defaulting**
instead of refusing: it takes a base model and returns the model for a role,
falling back to the base when a role is unset, so it works out of the box.

```
models.json:  { "reviewer": "gpt-5.4-mini", "fixer": "gpt-5.4-nano" }
```

Loaded from `AGENTPIPE_MODELS` when set, otherwise every role is the base model.
Defaulting a model is safe (a wrong price silently mis-bills; a wrong-but-present
model just runs), so this is the one config allowed to default, unlike `PriceMap`
which refuses. `models.json` is gitignored like `prices.json`. This is the I4 cost
lever: the fixer does narrow work and does not need the pricier model.

### `FIX_RULES` and the fixer pack

`FIX_RULES`: a stable system prompt (cache-friendly, never interpolated) asking for
the `--- file` / `--- end` patch format the builder already uses, so the fixer's
reply parses through the existing `parse_edits`/`apply_edits` with no new parser.
The fix pack carries only the one finding being fixed and the current contents of
the allowed files. Never past attempts, never the review history: rebuild not
accumulate, at the exact place the studied pipeline went quadratic.

### The loop: `run_review_fix`

`run_review_fix(ticket, repo, client, models, files, max_rounds, min_severity)`,
a plain loop (no LangGraph), one finding per round:

1. **Review** the current code via Stage 1's `run_review` (reviewer model from the
   map). Malformed reviewer output is refused by `parse_findings`; the loop catches
   `ReviewError` and retries the review up to a small cap, then stops if it will not
   parse.
2. **Pick** the single worst finding at or above `min_severity`. None -> clean,
   stop.
3. **Snapshot**: read the current contents of the allowed files into memory and
   record which paths already exist (so a created file can be deleted on revert).
4. **Fix**: `client.call(fix_pack(finding, code), model=models.for_role("fixer"),
   role="fixer", attempt_kind="review_fix", attempt_index=round)`, then
   `parse_edits` and `apply_edits(allowed=ticket.files_hint or None)`.
5. **Re-validate** with `run_checks(ticket.validation, repo.root)`.
   - If it still passes: mark the finding `fixed`, keep the change, continue.
   - If it now fails or errors: **revert** from the snapshot (rewrite captured
     contents, delete any newly-created file), mark the finding
     `reverted (broke validation)`, and continue to the next round so one bad fix
     does not end the run. The reverted round still cost a call; that is recorded.
   - A fixer reply that will not parse (`PatchError`) is marked `unfixable
     (bad patch)` and skipped, not retried forever.
6. Repeat until clean, out of actionable findings, or `max_rounds` reached.

`ReviewFixResult`: rounds run, each round's finding and its outcome
(`fixed`/`reverted`/`unfixable`), the per-round validation, and total cost across
every review and fix call. Plus `report_review_fix` for a human summary.

`max_rounds` and `min_severity` are hypotheses, parameters with docstrings naming
what would settle them, not tuned constants. `min_severity` defaults to `medium`
for the fixer (acting on every `low` nitpick costs money and churns the diff for
little gain), a labelled guess that Stage 3's table settles by showing which
severities' fixes actually survive.

### CLI (`run.py`)

Add `--fix` (implies review, then the fix loop) and `--models <path>` (or
`AGENTPIPE_MODELS`). `--review` stays advisory (Stage 1). After a successful loop,
`--fix` runs `run_review_fix` on the written files and prints `report_review_fix`.
Opt-in, because it adds cost and writes to the tree.

### Tests (`tests/test_review_fix.py`)

Self-contained fakes (each test file owns its fake, the lesson from Stage 1's CI
break). A fake that returns a scripted sequence of reviewer and fixer replies keyed
by role, so review and fix can be driven independently. Cases:

- a finding, a good fix, validation still passes -> outcome `fixed`, code changed.
- a fix that breaks validation -> `reverted`, and the file on disk is byte-for-byte
  the pre-fix content (the revert guard, the heart of the stage).
- a fix that *creates* a file then breaks validation -> the created file is deleted
  on revert (the in-memory-snapshot edge case).
- clean review (`[]`) -> no fix attempted, stop.
- only sub-threshold findings -> nothing acted on, stop.
- the round cap is respected (a finding that never clears stops at `max_rounds`).
- a malformed reviewer reply -> refused, retried, then a clean stop.
- an unparseable fixer reply -> `unfixable`, skipped, loop continues.
- the fixer call uses the fixer model from the map (`role="fixer"`,
  `attempt_kind="review_fix"`), so routing and Stage 3 attribution are real.

### Verification

- `pytest -q` green including `tests/test_review_fix.py`, run with the bare
  `pytest` console script (as CI does), not only `python -m pytest`.
- A real run: thin `truncate` code, `--fix`, watch the reviewer flag the ellipsis
  bug, the fixer repair it, validation stay green, and the outcome read `fixed`,
  with the fixer call recorded under `role='fixer'` at the cheaper model. Then a
  deliberately unfixable case to watch a revert leave the code untouched.
- CI stays green (real Postgres). No schema change this stage; that is Stage 3.

### Deferred to Stage 3

Recording findings and outcomes to a `review_findings` table, and the views that
answer "is the reviewer worth its cost". Stage 2 returns outcomes in memory and
prints them; persistence is the measurement stage.

## Stage 3: the audit table (built)

The instrumentation that answers the open questions instead of hoping. Records both
the fix loop's outcomes and advisory `--review` findings (agreed with the user).

- Migration `migrations/003_review_findings.sql`: table `review_findings`
  (`run_id`, `task_ref`, `round`, `severity`, `file`, `line`, `issue`, `outcome`,
  `model`, `call_key`, `created_at`). `outcome` is one of `reported` (an advisory
  review finding), `fixed`/`reverted`/`unfixable` (the fix loop). `model` is the
  model responsible (reviewer for `reported`, fixer otherwise), so grouping by it
  answers the routing question. `call_key` is a soft link to the responsible
  `model_calls` row, not a foreign key (model_calls has none and telemetry is
  best-effort). Append-only, no unique index: a log, not a cache. Plus two views:
  `finding_outcomes` (severity x outcome counts) and `fixer_reliability` (fixed vs
  reverted vs unfixable per fixer model, with a `fix_rate_pct`).
- `findings.py`: `FindingStore` is a port (abstract + `InMemoryFindingStore` +
  `PostgresFindingStore`), contract-tested against both in
  `tests/test_finding_store_contract.py`, the same guardrail as `CallStore`. A
  `FindingRow` validates severity/outcome at construction (unrepresentable invalid
  state). `record_review_findings` / `record_fix_findings` turn a returned
  `ReviewResult` / `ReviewFixResult` into rows, deriving run_id, model, and call_key
  off the already-recorded call, so the loops stay database-free. Recording swallows
  its own errors (`_safe`): the audit can never fail a run.
- The CLI records after `_review` / `_review_fix`, best-effort.
- Proven on real runs: nano fix `unfixable`, mini fix `fixed`, one advisory
  `reported`. `fixer_reliability` then read mini 100% / nano 0% fix rate straight
  from the table. The routing question, answered from data.
- With `model_calls` (per-role cost) plus `review_findings`, the tables answer: does
  the reviewer find useful things (findings vs `fixed`); does the cheap fixer work
  (`fixer_reliability`); which severities survive; is a wrong review degrading code
  (`reverted`, caught by the guard); cost (join on `call_key`).

## What to expect, honestly

- Whether the reviewer finds real issues or nitpicks is genuinely unknown; the
  audit table (Stage 3) is exactly how we find out, on real runs.
- The revert guard (Stage 2) means the worst case is "spent money, changed
  nothing", never "degraded working code".
- Review adds cost; that cost is the motivation for Layer 6 (the eval gate before
  review), which is the next layer.

## Risks

- A wrong review degrading working code: guarded by re-validate + revert (Stage 2).
- Oscillation (fix A creates finding B): guarded by the round cap (Stage 2).
- Cost blowup at full context: guarded by opt-in, severity threshold, and the round
  cap; fully addressed later by Layer 6.
- Malformed JSON from the reviewer: strict `parse_findings` refuses; Stage 1
  surfaces it, Stage 2 retries it.
- The fixer touching files outside the agreed set: `apply_edits`' allowed-set
  already refuses it (Stage 2).
