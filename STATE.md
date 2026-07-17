# STATE

Where this actually is, as of 17 July 2026. Read after CLAUDE.md and PLAN.md.

CLAUDE.md is the rules. PLAN.md is the design. This is the situation.

## Built

Layers 0, 1 and 2. 106 tests, all passing.

```
telemetry.py   the seam. Every model call goes through MeteredClient.call()
ticket.py      the contract. Refuses vague tickets before anything is billed
repo.py        git ls-files, plus which files are worth paying for
pack.py        assembly, ordered by volatility so the cache prefix stays stable
patch.py       parses the model's reply, refuses ambiguity, writes to disk
builder.py     the wiring. Ticket in, files out. One shot, no loop
run.py         CLI. Dry run by default
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

`cache_hit_pct` is 0. Layer 1's whole argument (stable content first, never
interpolate into RULES, the prefix test) is **unproven**. Every run has either
been a first call or a replay from our own table. The 90% cached-input discount
has never appeared on a real invoice. First honest test is Layer 3, when attempt
2 sends the same prefix plus feedback.

`avg_thinking` is 0. This model spends no reasoning tokens here, so the output
budget in `builder.py` solves a problem that has not occurred. Still correct to
have. Was not the fix it was sold as.

## Next, in order

**1. A ticket that is not already done.**
`tickets/TASK-1.md` asks for `prices.example.json`, which exists and is correct.
The pipeline rewrites it anyway, 20 lines to 20 lines. Do not `--apply` it.
Write something real: a CI workflow running pytest, or the tracing gap below.

**2. The stale ticket hole.**
Nothing in the pipeline asks "is this already true?". The agent always does
something, because it was given something to do. Validation cannot catch it:
`pytest -q` passes whether or not the work was needed. This is a design gap, not
a bug, and it is more interesting than anything fixed so far.

**3. Layer 3, the loop.**
Where LangGraph finally earns its place, where the cache claim gets tested, and
where A1.5's idempotency tension springs for real. See PLAN.md.

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

Both recorded in PLAN.md under Layer 0, repeated here so they are not missed:

**Spans go nowhere.** `trace_id` and `span_id` write as all zeros. No tracer is
configured, so OTel's no-op default accepts every span and discards it. The
ledger is real, the trace is not. Fix at Layer 3.

**Contract tests skip without a DSN.** `tests/test_store_contract.py` only runs
against Postgres when `AGENTPIPE_DSN` is set, so a bare CI job would skip the
tests that exist to catch store divergence. That is the same hole as the bug they
were written for: a promise nobody checks. CI needs a Postgres service container,
not a skip.

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

Four real bugs, all the same one: **the system reported success while quietly not
doing the thing.**

- pytest reported 95 passing from a test file in the wrong folder
- the in-memory store kept content, the Postgres one dropped it, and fifteen
  tests passed against the double
- a billed call returning nothing was recorded as 'ok' and became a permanent
  cache hit
- the contract tests wrote fixture rows into the live table and ratio_by_role
  averaged them in

None errored. All lied. This is why the meter exists, and it is why Andrew's
70,000 sat there unnoticed: nothing was broken, it just cost money.

When something looks fine, that is not evidence. Check the table.
