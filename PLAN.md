# agentpipe: implementation plan

## Credit

The architecture here is not original to me. The two ideas it rests on, that the
workflow owns the loop and that context is rebuilt from real state rather than
accumulated, come from a design written by Andrew Onwe (@DrewCodesIt), who measured the
70,000-to-100 ratio that started all of this. The harness split in Layer 5 is his
too. What is mine: the layering, the build order, the telemetry in Layer 0, and
the argument that measurement should come before any of it.

## What we are building

A coding agent that takes a written ticket, writes the code, proves it works, and
opens a pull request. A human decides what to build and whether to merge.
The agent does the middle part.

## Why it is built in this order

Every layer below is useless on its own and useful with the one before it. That
is deliberate. If we built the whole thing at once and it misbehaved, we would
have eight suspects and no way to tell which one lied.

The rule for the whole build: **one layer at a time, working and understood
before the next one goes on top.**

---

## The three ideas the whole thing rests on

Everything else is detail. If you remember only these, you can rebuild the rest.

### 1. The workflow owns the loop. The agent only thinks.

The agent does not decide when to retry, what to remember, or when it is done.
It gets a pack of text, it returns a pack of text. Everything else is our code.

**Why:** an agent that controls its own memory will fill it. Not maliciously,
just because more context always looks helpful in the moment. Take the decision
away and the problem cannot form.

### 2. Rebuild, never accumulate.

On attempt 4, we do not hand the agent attempts 1, 2 and 3. We go back to the
repository, read what is actually there now, and build a fresh pack.

**Why:** if attempt N is built from attempt N-1, the input grows every pass, and
five attempts costs far more than five times one attempt. Rebuilding from real
state means attempt 4 costs about the same as attempt 1. It is also more
accurate, because the repo is the truth and a log of what we tried is not.

### 3. One door.

Exactly one function in the codebase may call a model. If there were two, the
telemetry would be a suggestion rather than a fact.

**Why:** this is the difference between "we log our costs" and "our costs are
logged." Only one of those survives a busy Tuesday.

---

## The layers

### Layer 0: the meter (done)

**Goal:** know what a model call costs, and never pay for the same one twice.

**Tasks**
- [x] `MeteredClient` as the single seam for model calls
- [x] OpenTelemetry spans using the standard GenAI attribute names
- [x] `model_calls` table, append-only, unique on idempotency key
- [x] Cost from a config-loaded price map, in Decimal
- [x] `ratio_by_role` and `run_costs` views
- [ ] **Your task:** route real calls through it for a week, change nothing else

**Decisions and why**

*Idempotency key excludes run_id, includes a hash of the context pack.*
Two runs of the same task, same attempt number, byte-identical input, are the
same call. The second should be free. And if the pack changed, it is genuinely
different work and should be paid for. The hash gives us both for free.

*Only successful calls are replayed.* Caching a failure would turn a five-second
network blip into a permanent one. The task would be wedged forever and retrying
could never fix it, because retrying is the exact thing the cache suppresses.

*The meter can never fail the run.* A meter that takes down the thing it measures
gives you two problems instead of one.

*Prices come from a file and the code refuses to guess.* A confidently wrong cost
dashboard is worse than no dashboard, because you will believe it.

**Path:** E5 (agent memory in Postgres), P3 (cost and latency tracking, OTel
semantic conventions), A1.5 (idempotency, safe retries).

---

### Layer 1: the context builder

**Goal:** turn a ticket into a pack of text, the same way every time.

**Tasks**
- [ ] Define the ticket contract: goal, constraints, acceptance criteria, validation commands
- [ ] Reject tickets that do not meet it, before any model is called
- [ ] Rank and select likely-relevant files
- [ ] Assemble the pack in a fixed order: stable content first
- [ ] Prove determinism: same inputs, same `pack_hash`, in a test

**Decisions and why**

*The builder is a pure function.* Same inputs, same output, every time. This is
what makes the pack hash meaningful, which is what makes idempotency work, which
is what makes Layer 4 possible. Determinism is not tidiness here, it is load-bearing.

*Stable content goes first in the pack.* Repo rules, conventions, anything
constant. Providers bill cached input at a large discount, and cache hits depend
on the *front* of your prompt being byte-identical. Put the changing part last
and you get the discount for free. This is the single cheapest cost win available
and it is a consequence of good structure rather than an optimisation.

*A bad ticket fails here, not at the model.* Rejecting a vague ticket costs
nothing. Discovering it was vague after five attempts costs real money.

**Path:** P1 (context engineering: "identify every place context is implicit,
redesign it to be explicit"). This layer is that exercise, on a real system.

---

### Layer 2: the builder node

**Goal:** one shot. Pack in, patch out. No loop yet.

**Tasks**
- [ ] LangGraph graph with a single node
- [ ] Node calls `MeteredClient`, never OpenAI directly
- [ ] Apply the returned patch to a working tree
- [ ] Run it on something trivially small and watch it work

**Decisions and why**

*No loop yet, on purpose.* Loops hide bugs. If the one-shot case is wrong, the
looped version is wrong five times and much harder to read.

*Node calls the seam, not the SDK.* First real test of the one-door rule. If it
holds when it is inconvenient, it will hold.

**Path:** W4 (graph-based agent flows), the smallest possible slice of it.

---

### Layer 3: the loop

**Goal:** run validation, feed real failures back, stop at a limit.

**Tasks**
- [ ] Validation runner that executes the ticket's declared commands
- [ ] Conditional edge: pass, or rebuild and retry
- [ ] Attempt counter that survives a process crash
- [ ] Hard stop at N attempts, marked blocked, human notified

**Decisions and why**

*Validation output is the truth. The agent's claim is not evidence.* Agents say
"all tests pass" when they have not run the tests. This is not dishonesty, it is
a language model completing a plausible sentence. The only defence is to run the
tests yourself and believe only the exit code.

*The retry rebuilds the pack, it does not append the failure.* Idea 2, at the
exact place it matters most. This is the difference between five attempts costing
5x and 15x.

*The counter must survive a crash.* Here is where you feel A1.5 bite: the agent
commits, the process dies, and the counter says 3 when reality says 4. Retry
re-does work that already landed and pays for it twice.

**Path:** W4 (conditional loops and retries), A1.5 (partial failure, timeouts).
The path says the tensions planted in A1.5 get sprung in W4. This is that.

---

### Layer 4: replay

**Goal:** answer "why did this run cost $12" without re-running it.

**Tasks**
- [ ] Store the pack inputs as events, not the pack itself
- [ ] Rebuild any historical pack from its events
- [ ] Test: replayed pack hash equals the original

**Decisions and why**

*Store the inputs, not the output.* If the builder is a pure function, the inputs
are enough. Storing packs would be storing a derived value, and derived values go
stale.

*Replay costs zero tokens.* This is the whole point. Without it, debugging an
expensive run means paying for it again to find out why it was expensive.

**Path:** A2 (event sourcing: "every agent decision becomes a replayable event").
Layer 1's determinism is what makes this possible, which is why it comes first.

---

### Layer 5: reviewer and fixer

**Goal:** a second opinion, and a loop that acts on it.

**Tasks**
- [ ] Reviewer role, structured output, not prose
- [ ] Fixer loop that repairs against findings
- [ ] Fixer pack contains findings and current diff only, never past attempts
- [ ] Route roles to different models

**Decisions and why**

*Structured findings, not prose.* If the fixer has to parse "this looks a bit
off," it will guess. A severity field and a file path are unambiguous.

*Cheap model for narrow work.* A two-line fix does not need your most expensive
model with the whole repo in context. This is the biggest single cost lever in
the system, bigger than anything context engineering buys you.

*The fixer never sees its own history.* Idea 2 again. This is the exact place
the original pipeline we studied went quadratic.

**Path:** P4 (harness engineering: guides, sensors, data context), I4 (model
routing, cost and latency).

---

### Layer 6: the eval gate

**Goal:** stop bad work before it reaches the expensive part.

**Tasks**
- [ ] Small eval dataset from real tickets
- [ ] Judge scores the patch before review
- [ ] Below threshold: back to the builder, do not open a PR
- [ ] Track judge cost separately, it must earn its place

**Decisions and why**

*The gate sits before review, not after.* Review plus repair is five reviewer
calls and five fixer calls at full context. That is the most expensive stretch in
the pipeline. One judge call that kills all ten is the best ratio in the system.

*Judged, not trusted.* Same principle as validation. The difference is that
validation catches "it does not run" and the judge catches "it runs and it is
wrong," which no test suite you did not write can catch.

**Path:** P2 (evals as CI/CD gates, LLM-as-a-judge, harness sensors). The path
frames evals as quality. Here they are also the cheapest cost control you have.

---

### Layer 7: durability

**Goal:** a crash resumes instead of restarting.

**Tasks**
- [ ] Temporal workflow owns the loop, limits, and attempt state
- [ ] Every model call becomes an activity
- [ ] Graph nodes become activities, unchanged
- [ ] Test: kill the worker mid-loop, confirm resume

**Decisions and why**

*Temporal outside, LangGraph inside.* Two different jobs. Durable state, retries
and timers are a solved problem and Temporal solved it. Agent reasoning is not,
and LangGraph is a reasonable answer. Neither is good at the other's job.

*This layer is last despite being foundational.* It is the hardest to debug and
the easiest to add to something already working. Adding durability to a correct
system is a refactor. Debugging a system that is both wrong and durable is a bad
week.

*It is also a cost fix, not just reliability.* Without it, a crash at attempt 4
throws away four paid-for attempts. Tokens you already bought stay bought.

**Path:** W7 (durable execution, the two-layer Temporal + LangGraph pattern).

---

## The judgment call, stated plainly

A2 teaches "when is this NOT worth it," so it should be applied here too.

If this pipeline ends up running ten tickets a month, Layers 4 and 7 are
ceremony. Event sourcing and durable execution earn their complexity at volume
and under real failure, not on a side project. The honest version of this plan is
that Layers 0 through 3 are the system, 5 and 6 make it good, and 4 and 7 make it
industrial.

Build them anyway if the goal is learning them. Just be clear which goal you are
serving, because the two produce different systems.

## The open question

Layer 1 cannot be written until we know what codebase this operates on. The
validation commands are part of the ticket contract, and validation commands are
specific to a repo. That answer unblocks everything after Layer 0.

