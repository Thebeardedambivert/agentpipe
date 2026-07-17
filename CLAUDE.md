# CLAUDE.md

Operating context for agentpipe. Constraints and decision rules only.

## Stack

Python 3.14, OpenAI SDK, OpenTelemetry, psycopg 3, Supabase Postgres, pytest.
Windows dev machine, PowerShell. `src/` layout, installed editable.

## The one rule that matters

`MeteredClient.call()` in `src/agentpipe/telemetry.py` is the ONLY place that may
call a model. If you are about to `import openai` anywhere else, stop. That is
not a style preference, it is the invariant the entire project rests on.

## Constraints

- Never write model prices into code. They live in `prices.json`, loaded from
  `AGENTPIPE_PRICES`. `PriceMap.from_env()` raises rather than defaulting, on
  purpose. Do not "helpfully" add a fallback.
- Never look up current model prices and fill them in. You will be wrong and it
  will look right. Cyril fills that file by hand from the pricing page.
- Cost is `Decimal`, never `float`. It is money.
- Telemetry must never fail a run. `_safe_record` swallows store errors by
  design. Do not make it raise.
- Only successful calls are replayable. Caching an error would wedge a task
  permanently. See `MeteredClient.call`.
- `.env` and `prices.json` are gitignored. Never `git add -f` them. Never print
  their contents. Never paste a DSN or key into a commit, comment, or README.
- The idempotency guarantee is the unique index in `schema.sql`, not the Python
  check. Keep both.
- PowerShell writes UTF-8 with a BOM via `Set-Content -Encoding utf8`, which
  breaks `tomllib`. Use `-Encoding ascii` for any config file.

## Build order

Layers 0 through 7, defined in PLAN.md. Layer 0 is done. Do not implement a
later layer before its dependencies. In particular:

- No token budgets, caps, or context trimming until there is a real measured
  baseline. That is the entire thesis of the project.
- Layer 1 is blocked: the context builder needs to know which codebase this
  pipeline operates on, because validation commands are part of the ticket
  contract.

Read PLAN.md before proposing work on any layer. Each layer's "decisions and
why" section is binding, not commentary.

## Lessons paid for

Each of these cost a real run or a real bug. They are not style opinions.

**Do not second-guess an explicit human decision.**
The ticket's Files section is the answer, not a suggestion to improve on. The
first selector added its own picks on top of the ticket's, matched "repo" in the
goal "the file exists at the repo root" against repo.py and test_repo.py, and
spent 67% of the pack on two irrelevant files. The bug was not the matching. It
was arrogance. Where a human has stated a decision, implement it.

Corollary: when withholding context from the model, check it has a way to ask.
Trusting the hints is only safe because the pack carries the whole tree for ~39
tokens and RULES tells the model to speak up rather than guess.

**Do not invent tuned numbers.**
A score floor of 1.0 was added with the comment "tuned by nothing" and shipped
in the same breath. It silently selected zero files on a small repo. If a
threshold, weight, or limit is not derived from something in `model_calls`, it
is a guess wearing engineering clothes, and this project exists to argue against
exactly that.

Not a ban. Guessing is sometimes the only option. But then: say so in the name or
the docstring, make it a parameter rather than a constant, and write down what
number would settle it. A guess that admits it is a hypothesis. A guess that does
not is a bug with good manners.

**A ceiling is not a quota.**
`max_files=5` means at most five, not find me five. The first version filled the
slot with anything scoring above zero. Limits bound the worst case; they are not
targets to reach.

**Prefer removing the clever thing to tuning it.**
The rarity weighting was sound. The threshold on top was not. When a fix has a
defensible core and a speculative extra, ship the core and delete the extra. The
extra can come back when there is a number demanding it.

**A test double that disagrees with the real thing is worse than no test.**
`InMemoryCallStore` kept content. `PostgresCallStore` dropped it and replayed
empty strings. Fifteen idempotency tests passed against the double while
production silently broke on every second run, and the failure surfaced as
"empty reply" and looked like a model problem for an hour.

Where a port has more than one implementation, the contract is tested against
all of them, in one parameterised file, not per-implementation. See
`tests/test_store_contract.py`. Adding a third store means adding it to that
list, not writing new tests for it.

**An invalid state you can build is an invalid state you will build.**
This is the one that makes the others cheaper. `CallRecord(status="ok",
content="")` was constructible, so it got constructed, stored, read back, and
replayed. Four sites could each have caught it. None did, because none of them
was *the* site.

Guarding at every use site is how you get a codebase made of paranoia that still
misses the fifth site. Validate at construction: one place, every path in and
out, including paths nobody has written yet. `__post_init__` on a frozen
dataclass costs three lines and retires four guards.

Before adding a check, ask whether the thing being checked for could instead be
made unrepresentable.

**No exception is not success.**
Every silent failure in this project has been code asking "did it throw?" instead
of "is it true?". The pytest cache reporting 95 passing tests from a file in the
wrong folder. `_safe_record` swallowing a paused database. The in-memory store
agreeing with itself. A row marked 'ok' with nothing in it. None errored. All
lied. Assert the postcondition, not the absence of a stack trace.

**A cache that does not store the result is not a cache.**
It is a log of what happened, wearing a cache's clothes, and it will hand
callers an empty answer while reporting success. If `find()` cannot return
everything `record()` was given, do not claim to replay.

**Nothing writes to the measurement table except real work.**
The contract tests wrote fixture rows into a live database, tagged
task_ref='TASK-1' with invented token counts, and ratio_by_role started averaging
them in with real calls. The tests for the meter corrupted the meter. Preflight
did the same thing on a smaller scale, leaving a probe row behind on every run.

Anything that touches a real store from a test or a diagnostic: tag it
unmistakably, and delete it afterwards in a finally or an autouse fixture, so it
cleans up on failure too. There is no version of "it is only a few rows" that is
true when the rows are your evidence.

**A bug found in a real run gets a regression test naming the real run.**
Not a generic test. One that says what happened, what it cost, and why the fix is
shaped the way it is. See `test_hints_are_authoritative_not_advisory`.

## Testing

`pytest -q`. 15 tests. They do not test whether OpenAI works, they test the three
guarantees: never pay twice, the cost number is right, the meter never kills the
run. Any new code touching the seam needs a test at that level, not a mock that
proves the mock works.

`python -m agentpipe.preflight` before trusting any number in the table. The free
Supabase tier pauses after 7 days idle, and a paused project fails silently
through `_safe_record`.

## Attribution

The architecture is Andrew Onwe's (@DrewCodesIt). Rules about the workflow owning
the loop and rebuilding context rather than accumulating it are his. Do not strip
the credit sections from README.md or PLAN.md.

## Style

- No em dashes in any file, comment, commit message, or docstring.
- Comments explain why, not what. The why is the transferable part.
- Sentence case in headings.
