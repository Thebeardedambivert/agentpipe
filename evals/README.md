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

## What the numbers are not

They are counts, never rates. At this size one flipped verdict moves a percentage
by double digits and still reads as a measurement. Nothing here is a pass mark;
whether `--gate` has earned its authority is a human call made while looking at
the counts.

Eight cases, half of them constructed, is not a measurement of accuracy. It is a
smoke test for a sensor, and a place to put the next real disagreement.
