# The real-world trial

How agentpipe gets pointed at codebases it did not grow up with, and how the
result gets read. Written before any repository was screened, so the rules are
constraints rather than a description of what was already picked.

## Why this exists

One real third-party run (boltons #301, twice) says almost nothing on its own. It
found a licence hazard, a cost problem and a format bug, all of which were worth
finding, but a single case cannot say whether the pipeline works. That is the same
argument Layer 6 Stage 3 made about the judge: an instrument that has only ever
been tested on one case has not been calibrated.

It is also the second thing this buys. STATE.md's most serious open gap is that
the eval dataset has no harvest path, because `TASK-GATE` was run without capturing
its files and a real judge verdict is therefore unrecoverable. Every run here
produces exactly what a case needs. If the files are captured as they go, the gap
closes as a side effect rather than as a project.

## The selection rule, fixed in advance

The failure mode this rule exists to prevent is the obvious one: whoever picks the
tickets will pick tractable ones, without meaning to, and the trial will then
measure the picker rather than the pipeline. So the rule is fixed first and takes
what it gives.

**A repository qualifies if all of these hold:**

1. Pure Python, no third-party runtime dependencies.
2. Its own test suite runs in under 60 seconds on the dev machine. (A RAM
   constraint, and a real one: this machine has already been taken down once.)
3. A permissive licence (MIT, BSD, Apache).
4. It has open issues that report behaviour, not feature requests.

**An issue qualifies as a ticket if all of these hold**, taken in the order the
tracker returns them, newest first, no skipping:

1. It contains a self-contained snippet that is supposed to reproduce.
2. That snippet **still fails on today's HEAD when actually run**. Not "looks like
   it would": run it, keep the output.
3. The issue's own text implies a fix touching at most two files. Judged from what
   the reporter wrote, never from attempting the fix first.

If the first three candidate issues in a repository all fail rule 2, that
repository is dropped and **that fact is recorded**, because "most reported bugs
are already fixed" is itself a finding about where this pipeline would be used.

**The ticket is written from the issue text alone**, before any attempt to solve
it, and must not telegraph the fix. Copying a maintainer's suggested patch into the
constraints would turn the trial into a typing exercise.

## Pre-registration

For each case, before the run: the ticket, and a written prediction of which rung
of the ladder it will reach and why. Committed first, then run.

Not ceremony. Without it, every outcome gets explained after the fact, which is how
a project convinces itself it knew all along. A wrong prediction that is on the
record is worth more than a right one that was never written down.

## The ladder, so a run is scored rather than judged

A single pass/fail hides where the thing broke. boltons #301 is the reason: it
would have scored 4 of 5, or "80% success", on a patch that did not fix the bug.

| rung | question | boltons #301 |
|---|---|---|
| L0 | did the reply parse? | yes, after the terminator fix |
| L1 | did the patch apply, SEARCH matching exactly? | yes |
| L2 | did validation pass? | yes |
| L3 | **is the reported bug actually fixed?** | **no** |
| L4 | was nothing else touched? | yes |

L3 is the only rung that is about the work. The others are about the machine. A
run that reaches L2 and fails L3 is this project's founding thesis reproduced, and
it must never be reported as a partial success.

## How the result gets read

- **Counts, never rates.** At five cases one flipped verdict moves a percentage by
  twenty points and still reads like a measurement. Same rule as `judge_accuracy`.
- **Every run is recorded, including the ones that go badly.** Especially those: a
  trial that only remembers its good runs is the survivorship bias STATE.md already
  names, in a new costume.
- **Where it broke is the output, not whether it worked.** Format, matching, file
  selection and model quality are four different failures with four different
  fixes, and the ladder is what separates them.

## Screening log, 27 July 2026

Recorded because a trial that only lists the repositories it kept is already
selecting for the answer it wants.

| repo | outcome |
|---|---|
| `mahmoud/boltons` | already run twice, issue #301, kept as case 0 |
| `pytoolz/toolz` | qualifies. Issue #626 is the newest bug with a snippet, and it reproduces |
| `Suor/funcy` | qualifies. #162/#160/#145/#123 are features or docs, so the first bug candidate is #108, and it reproduces |
| `more-itertools/more-itertools` | **dropped.** All seven open issues are feature requests. There was no bug to take |
| `pyparsing/pyparsing` | qualifies on the rules. Held back on cost, see below |

Three things that went wrong in the screening itself, kept because they are the
part that would otherwise be quietly tidied away:

- **A rule was broken by the person who wrote it.** The first pyparsing check ran
  a snippet invented here rather than the one in the issue, which rule 1 forbids
  precisely because of what happened next: the snippet exited 1 and was reported
  as "still reproduces", when in fact it demonstrated *correct* behaviour
  (`leave_whitespace()` is supposed to stop skipping the space). An exit code was
  read as an answer without reading the output. That is the project's signature
  bug, committed inside the document that warns about it.
- **funcy needed a test-only dependency** (`whatever`, by funcy's own author).
  Rule 1 bans third-party *runtime* dependencies and funcy has none, so this is
  within the letter of the rule, and it is recorded rather than glossed because it
  is the kind of thing that quietly becomes "well, one more package".
- **toolz has a pre-existing failing test.** `toolz/tests/test_package.py::
  test_has_version` fails on a bare clone, unrelated to anything here, and it is
  its own open issue (#612). Validation therefore names the relevant test file
  rather than running the whole suite, the same as the boltons ticket. A ticket
  whose validation is red before the work starts cannot tell you anything.

## Cost, measured before spending

Per attempt, at the recorded `gpt-5.4-mini` prices, dominated by how much source
goes into the pack:

| case | file(s) | tokens | projected |
|---|---|---|---|
| toolz #626 | `itertoolz.py`, 28.7 KB | ~7,200 | ~$0.007 |
| funcy #108 | `_inspect.py` + `funcs.py`, 11 KB | ~2,800 | ~$0.003 |
| pyparsing #633 | `core.py` **260 KB** + `helpers.py` | ~76,000 | **~$0.06** |

pyparsing is twenty times the funcy case for one attempt, because the whole of
`core.py` goes into the pack to change a few lines of it. That is the whole-file
*input* problem, which search/replace did nothing about: it fixed output, and this
is the other half. Worth stating plainly, because it is the first case where the
context builder's cost, rather than the patch format's, is the thing that hurts.

## Pre-registered predictions

Written before any of these were run.

**toolz #626: reaches L3.** The behaviour is fully specified by two lines of
observed output, the change is small and local, and the acceptance check tests
exactly what the issue describes. If anything goes wrong it will be L4: the model
touching the docstring or the lazy branch while it is in there.

**funcy #108: fails at L3, reaching L2.** Determining how many arguments a
built-in method descriptor takes is not a small fix; it needs a fallback path for
callables that have no introspectable signature, and "the tests pass" is easy to
reach without getting that right. This is the case most likely to reproduce the
boltons pattern of green tests over unfinished work.

**pyparsing #633: fails at L1 or L3.** The bug lives in the interaction of two
features across a 260 KB file. The most likely failure is the SEARCH text not
matching, because the model has to quote from a file far larger than anything it
has been asked to quote from so far.

**Across all three: at least one L2-pass/L3-fail.** If every case reaches L3, the
right conclusion is that the issues were too easy, not that the pipeline is good.
That is the same discipline the judge's eval dataset was held to, and it is stated
here so it cannot be renegotiated afterwards.

## Results, 27 July 2026

Two cases run, one attempt each, `gpt-5.4-mini`, `--apply`. pyparsing held back on
cost. Counts, not rates: this is three cases including boltons.

| case | L0 parse | L1 apply | L2 tests | L3 bug fixed | L4 nothing else | cost |
|---|---|---|---|---|---|---|
| boltons #301 | yes | yes | yes | **no** | yes | $0.008044 |
| toolz #626 | yes | yes | yes | **yes** | yes | $0.007622 |
| funcy #108 | yes | yes | yes | **no** | **no** | $0.004744 |

**Both predictions were right, which is worth less than it looks.** toolz reached
L3 as predicted, funcy failed at L3 as predicted. Two predictions is not a track
record, and the funcy one was easy to make: "introspecting built-in method
descriptors is harder than it looks" is not a bold call.

**The finding is funcy, and it is worse than boltons.** The patch passes all 205
tests, does not fix the reported bug (`rcurry(str.endswith)` raises the identical
`ValueError`), and **quietly breaks something else**. It rewrote

    self_set = {func.__init__.__code__.co_varnames[0]}

as a `getattr(func.__init__, '__name__', None)`, which is the string `'__init__'`,
not the name of the first parameter. So `self` stops being stripped from a class's
argument spec:

    original : names={'a', 'b'},         req_names={'a'}
    patched  : names={'a', 'self', 'b'}, req_names={'a', 'self'}

Nothing in funcy's suite covers it. boltons was green tests over work not done;
this is green tests over work not done **plus** a silent regression, and the only
thing that caught it was a human reading the diff. That is the L4 rung having no
automated check behind it, stated in the ladder and now demonstrated.

**What the three cases say about where the pipeline breaks.** L0 and L1 held every
time, including a two-file reply, which is the first this project has ever
received and which the terminator fix was written for without having seen one.
Format and matching are no longer the weak point. **L3 is**, and L3 is the model,
not the machine: in all three cases agentpipe did its job and the patch was wrong.

**A caveat on toolz, recorded rather than celebrated.** The issue is from a
public tracker, has an open pull request against it (#629), and the fix is two
lines. A model that has seen the repository, the issue or the PR in training would
produce this without reasoning at all. That is the solution-leakage problem
SWE-bench has been repeatedly criticised for, it applies here, and nothing in this
trial controls for it. The funcy case is weak evidence against leakage mattering
much (that issue is public and open since 2021, and the model still failed), but
weak is the right word.

**Cost.** Three real third-party runs for $0.020410 in total. Cost is not what
limits this trial; the limit is how many issues can be screened and scored by
hand.

## What would falsify the current design

Stated now, so it cannot be softened later:

- Repeated L0/L1 failures would mean the format is still wrong, not the model.
- Repeated L2-pass/L3-fail would mean validation-as-truth is too weak on real
  repositories, which is the argument for the judge and for item B.
- Repeated L4 failures would mean search/replace did not deliver the safety it was
  adopted for.
- If the file selector picks wrongly on repositories with unfamiliar layouts, that
  is the first real evidence about the deliberately stupid selector, and the first
  number that could justify changing it.
