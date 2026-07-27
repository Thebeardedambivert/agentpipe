# The eval dataset

Layer 6 Stage 3. This is how we find out whether the judge is right, now that
Stage 2 lets it send the builder back to work.

```
python -m agentpipe.evals --dry-run     # validate every case, free
python -m agentpipe.evals               # grade the judge, real calls
python -m agentpipe.evals --repeat 5    # is it stable, or does it flip?
```

## What a case is

```
cases/<name>/
  ticket.md      a real ticket, parsed by Ticket.from_file. No second parser.
  code/          the files, exactly as they were. Read through the real Repo.
  case.json      provenance, which files the judge is shown, and the labels.
```

A label says what is true of this code, for one criterion, and why. Two states,
where the judge has three: a labeller who is uncertain has not finished making the
case. `uncertain` is an answer the judge may give, never a ground truth it can be
measured against.

The label carries the criterion's text as well as its index, and the loader
refuses to run when the two disagree. That is not belt and braces. Without it,
reordering a ticket's acceptance bullets silently repoints every label and the
eval reports a confident wrong number, which is the shape of all five bugs in
STATE.md: nothing errors, everything lies.

## Real and constructed

`provenance` is `real` (harvested from a run that actually happened, with its
`source` task_ref) or `constructed` (written to probe a failure real runs will not
hand us). The report never merges the two.

Harvesting only real runs is survivorship-biased in the exact direction that
matters: the dangerous case is the judge saying *satisfied* about wrong code, and
by definition that is a case nobody noticed, so it never appears in a harvest of
runs you were happy with. Constructed cases aim at that quadrant directly. The
cost is that they were written by the same person who wrote the judge's prompt, so
they can be unrealistically easy or unrealistically hard, and either produces a
flattering or damning number that means nothing. Splitting the cuts is what keeps
that visible.

## Adding a case

The best cases come from surprises. When the judge disagrees with you on a real
run, that run is a case: copy the files it judged into `code/`, copy the ticket,
write down what you believe and why, and say which run it came from.

```
python -m agentpipe.evals --dry-run --case <name>
```

validates it for free before you spend anything.

## Paired cases

Six of the cases come in matched pairs: the same ticket, one copy of the code with
a bug and one without (`strip-suffix-buggy` / `-fixed`, and so on). A pair is a
controlled test. A judge that answers both halves the same way has not read either
of them, and no single case can show you that.

## What the numbers are not

They are counts, never rates. At this size one flipped verdict moves a percentage
by several points and still reads as a measurement. Nothing here is a pass mark;
whether `--gate` has earned its authority is a human call made while looking at
the counts.

Fourteen cases, eleven of them constructed, is not a measurement of accuracy. It is
a smoke test for a sensor, and a place to put the next real disagreement.

## How we know it is not too easy any more

The first version was eight cases and the judge scored 16 of 16. That looked like
good news and was not. Two checks showed why:

- [JudgeBench](https://arxiv.org/pdf/2410.12784), the standing benchmark for LLM
  judges, tops out at **64%** for the best model in the world. Scoring 100% is a
  statement about your exam.
- Re-running with `gpt-5.4-nano`, a model 3.7x cheaper, also scored **16 of 16**.
  A test that cannot separate those two cannot answer "which model should judge?",
  which is one of the reasons it exists.

After the expansion the judge fails a case, stably, and the two models separate.
That is the property to protect: **if a change ever takes the score back to
perfect, suspect the dataset before congratulating the judge.**

## Verify every claim before it becomes a label

While building the paired cases, two claims that came from memory turned out to be
false when executed. Both would have shipped as ground truth. Run the code. A
dataset nobody checks becomes a confidently wrong answer key, which is the failure
`PriceMap.from_env()` refuses to have.
