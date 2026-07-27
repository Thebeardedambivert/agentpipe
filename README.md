# agentpipe

**70,000 tokens of input to produce 100 tokens of output.**

That was a real measurement, from a real agentic coding pipeline, doing real
work. The agent was fine. It read the ticket, wrote the code, opened the pull
request. It just did it at a ratio that makes no sense.

The cause was structural. Every repair attempt rebuilt its prompt from the pull
request body, and every attempt wrote its own log back into that body. Attempt 2
read attempt 1. Attempt 3 read 1 and 2. Five attempts was not five times the
cost. It was closer to fifteen.

No error. No alert. The only symptom was money.

This repo is me building the thing properly, in the open, one layer at a time.

## The idea

A coding agent that takes a written ticket, writes the code, proves it works, and
opens a pull request. A human decides what to build and whether to merge.

Three rules hold the whole design up:

1. **The workflow owns the loop. The agent only thinks.** An agent that controls
   its own memory will fill it.
2. **Rebuild, never accumulate.** Attempt 4 is built from the repository as it is
   now, not from attempts 1 through 3.
3. **One door.** Exactly one function may call a model. Otherwise the telemetry
   is a suggestion rather than a fact.

## Status

| Layer | What | Status |
|---|---|---|
| 0 | Metered model client + event store | **done** |
| 1 | Deterministic context builder | **done** |
| 2 | Builder node, single shot | **done** |
| 3 | Validation loop + attempt counting | **done** |
| 4 | Event-sourced pack replay | deferred, see PLAN.md |
| 5 | Reviewer + fixer, harness split | **done** |
| 6 | Evals as the gate before review | **done** |
| 7 | Temporal wraps the loop | deferred, see PLAN.md |

Layers 4 and 7 are the industrial ones. They earn their complexity at volume and
under real failure, and PLAN.md says plainly that on a side project they are
ceremony until then.

Full reasoning for every layer, including each decision and why it was made:
**[PLAN.md](./PLAN.md)**. Current state and what is next: **[STATE.md](./STATE.md)**.

## Layer 0

The meter. Every model call passes through `MeteredClient.call()`, which:

- **refuses to pay twice** for a call it has already made, keyed on a content
  hash of the context pack
- **records what every call cost** as OpenTelemetry spans using the standard
  GenAI attribute names, plus an append-only Postgres table
- makes the call

Measurement only. No budgets, no trimming, nothing clever. Those come later, and
they come later on purpose: every fix for a cost problem is unverifiable until
you can see the number.

```python
from agentpipe.telemetry import MeteredClient, PostgresCallStore, PriceMap

client = MeteredClient(store=PostgresCallStore(), prices=PriceMap.from_env())

rec = client.call(
    messages=[{"role": "user", "content": "hello"}],
    model="gpt-5.1-mini",
    role="builder",
    attempt_kind="implement",
    attempt_index=1,
    task_ref="TASK-1",
)

print(rec.usage.ratio, rec.cost_usd)
```

Call it twice with the same inputs and the second one costs nothing.

### The number this exists to produce

```sql
select * from ratio_by_role;
```

70,000 in / 100 out is a ratio of 700. Until this view has rows in it, you are
designing against someone else's number.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

cp prices.example.json prices.json   # then fill in the real numbers yourself
export AGENTPIPE_PRICES=$PWD/prices.json
export AGENTPIPE_DSN="postgresql://..."
export OPENAI_API_KEY="sk-..."

psql "$AGENTPIPE_DSN" -f schema.sql
pytest -q
```

The price map ships with nulls, and `PriceMap.from_env()` raises rather than
defaulting. That is deliberate. A confidently wrong cost dashboard is worse than
no dashboard, because you will believe it.

## Tests

```bash
pytest -q
```

237 tests. The Layer 0 ones do not test whether OpenAI works. They test whether
the three guarantees hold: never pay twice, the cost number is right, and the
meter never kills the run. The rest cover the context builder, patch parsing, the
loop, the reviewer and fixer, and the judge, and run against a real Postgres in CI
on every push.

Two of them exist because of bugs found while writing the first version. The
interesting one: the original code replayed *any* cached call, including
failures. A five-second network blip would have been cached as a permanent
result, and no retry could ever have dislodged it, because retrying was exactly
what the cache suppressed. Caching success is safe. Caching failure is a trap.

## Layer 6: who judges the judge

Layer 6 puts a model in front of the expensive part: after the tests pass, a judge
grades the acceptance criteria no exit code can check, and with `--gate` a failed
criterion sends the builder back to work.

Which raises the obvious question, so `evals/` answers it. Eight labelled cases,
each a real ticket plus the code as it was plus a human's verdict on every
criterion, and a harness that grades the judge against them:

```bash
python -m agentpipe.evals --dry-run     # validate the dataset, free
python -m agentpipe.evals               # grade the judge, real calls
```

It reports counts and refuses to report a rate. At eight cases a percentage moves
by double digits on one flipped verdict and still reads like a measurement. The
two numbers it exists to produce are **false pass** (the gate waves wrong code
through) and **false block** (the gate burns a rebuild on correct code); the
second only started costing money once the judge was allowed to drive the builder.

First run on `gpt-5.4-mini`: 16 of 16 criteria agreed, and 80 of 80 across five
samples. That is recorded as a warning about the dataset, not a win for the judge.
An instrument that has never disagreed with its calibration has not been
calibrated, and the dataset's next job is to acquire a case the judge gets wrong.

## Credit

The architecture is not original to me. Rules 1 and 2 above come from a design by
Andrew Onwe (@DrewCodesIt), who also measured the 70,000-to-100 ratio that started this.
The harness split in Layer 5 is his.

Mine: the layering, the build order, Layer 0, and the argument that measurement
comes before any of it.

## License

MIT
