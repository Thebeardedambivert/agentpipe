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

## Stage 2: the fixer loop, and model routing

Consume Stage 1's findings and act on them, safely.

- `ModelMap` (in `config.py` or beside `PriceMap`), mirroring `PriceMap.from_env`
  but **defaulting** instead of refusing: unset roles fall back to the run's base
  model, so it works out of the box. `models.json` shape:
  `{ "builder": "gpt-5.4-mini", "reviewer": "gpt-5.4-mini", "fixer": "gpt-5.4-nano" }`.
  Defaulting a model is safe (unlike a wrong price), so this is the one config that
  may default. `models.json` is gitignored like `prices.json`.
- `FIX_RULES`: stable system prompt asking for the `--- file` / `--- end` patch
  format the builder already uses, given only the findings and the current code,
  never history (Idea 2, at the place the studied pipeline went quadratic).
- `run_review_fix(...)`, a plain loop: review, filter at `min_severity`, snapshot
  the allowed files, fix, `apply_edits` (allowed = `ticket.files_hint`), re-validate.
  If validation no longer passes, revert to the snapshot and mark the findings
  `reverted (broke tests)`: a fix that breaks the build is discarded, so the cycle
  can never leave working code worse. Otherwise mark `fixed`. Repeat up to
  `max_rounds`. Malformed reviewer output is refused and retried inside the loop.
- `max_rounds` is a hypothesis, a parameter with a docstring naming what settles it.

## Stage 3: the audit table

The instrumentation that answers the six open questions instead of hoping.

- Migration `migrations/003_review_findings.sql`: table `review_findings`
  (`run_id`, `task_ref`, `round`, `severity`, `file`, `line`, `issue`, `outcome`,
  `created_at`) plus a view summarizing findings by severity and outcome.
- The review path records each finding and its outcome. Recording is append-only
  audit, not a replay cache, so it does not need the `CallStore` idempotency
  contract, but it is tagged and tested against the live DB the same careful way
  (nothing writes to the measurement tables except real work, and diagnostics clean
  up after themselves).
- With `model_calls` (per-role cost, already recorded) plus `review_findings`, the
  table answers: does the reviewer find useful things (findings vs `fixed`
  outcomes); does the cheap fixer work (`fixed` vs `reverted` per fixer model); how
  many rounds and which severities; is a wrong review degrading code (`reverted`
  count, caught by the guard); oscillation (findings recurring across rounds); cost
  (the review-fix stretch in `model_calls`).

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
