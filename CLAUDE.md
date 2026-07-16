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
