# STATE

Where this actually is, as of 17 July 2026. Read after CLAUDE.md and PLAN.md.

CLAUDE.md is the rules. PLAN.md is the design. This is the situation.

## Built

Layers 0, 1, 2 and 3. 130 tests, all passing. CI (`.github/workflows/ci.yml`)
runs them against a real Postgres on every push.

```
telemetry.py   the seam. Every model call goes through MeteredClient.call()
ticket.py      the contract. Refuses vague tickets before anything is billed
checks.py      the staleness gate and the validation runner. Exit-code contract
repo.py        git ls-files, plus which files are worth paying for
pack.py        assembly, ordered by volatility so the cache prefix stays stable
patch.py       parses the model's reply, refuses ambiguity, writes to disk
builder.py     the wiring for one attempt. Ticket in, files out
loop.py        the LangGraph loop: build, validate, retry, resume. Layer 3
run.py         CLI. Dry run by default; --max-attempts runs the loop
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

**3. Layer 3 is built. Next is Layer 5 or 6.**
The loop, crash-safe resume, and the tracing tree are done; the cache claim is
proven (92%, above the ~1,024-token threshold; see the baseline). Per PLAN.md the
honest next steps are Layer 5 (reviewer and fixer, the biggest cost lever, via
model routing) and Layer 6 (the eval gate before review). Layer 4 (event-sourced
replay) and Layer 7 (Temporal) are the industrial layers: worth it at volume or
for the learning, not before. `checks.py` already seeded the validation runner:
the same check run before is the staleness gate, run after is the success check.

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
