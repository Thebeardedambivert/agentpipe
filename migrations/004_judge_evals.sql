-- Layer 6 Stage 3: who judges the judge.
--
-- Stage 1 built the judge. Stage 2 gave it command authority: with --gate, a
-- BLOCK sends the builder back to work and spends another attempt. That was done
-- knowingly, with the caveat written down: we handed an unmeasured sensor the
-- wheel. This table is what removes the caveat.
--
-- One row per criterion per sample: what a human labelled, what the judge said.
-- The two failures it exists to make visible, neither of which anything currently
-- catches:
--   - false pass   (labelled not_satisfied, judged satisfied)
--                  the gate waves wrong code through and the layer was theatre
--   - false block  (labelled satisfied, judged not_satisfied)
--                  the gate burns a rebuild attempt on correct code, every time
--
-- Before Stage 2 only the first mattered and it was advisory. After Stage 2 both
-- cost money on every run.
--
-- Append-only, like model_calls and review_findings: a log, not a cache, so no
-- unique index and repeated gradings are expected (that is what `sample` is for).
-- Run in the Supabase SQL Editor against an existing database, or let CI apply it
-- to a fresh one. Safe to run twice.

create table if not exists judge_evals (
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid        not null,

    -- Which labelled case, and where it came from. Provenance is stored rather
    -- than inferred from the name because the report and the view both split on
    -- it: constructed cases were written by the same person who wrote the judge's
    -- prompt, so averaging them in with real ones would flatter the number.
    case_name       text        not null,
    provenance      text        not null check (provenance in ('real', 'constructed')),

    -- The real run this case was harvested from (a task_ref), or null when the
    -- case was constructed. Traces a row back to the thing that actually happened.
    source          text,

    criterion_index int         not null,
    criterion       text        not null,

    -- The label: what a human says is true of this code. Two-state on purpose,
    -- where the judge has three. A labeller who is uncertain has not finished
    -- making the case, so 'uncertain' is an answer the judge may give and never a
    -- ground truth it can be measured against.
    expected        text        not null check (expected in ('satisfied', 'not_satisfied')),

    -- What the judge actually said. Three-state, because collapsing 'uncertain'
    -- into 'not_satisfied' would erase the distinction judge.py exists to draw:
    -- "I cannot tell" is a different fact from "this is wrong", and only one of
    -- them is fixed by a better prompt.
    actual          text        not null check (actual in ('satisfied', 'not_satisfied', 'uncertain')),

    -- Which draw this was. --repeat runs each case N times at distinct
    -- attempt_index values, so samples are genuinely new paid calls rather than
    -- cache replays, and a judge that flips on identical input becomes visible.
    sample          int         not null default 0,

    model           text        not null,

    -- The identity of JUDGE_RULES when this grading happened. Load-bearing: rows
    -- from before and after a prompt edit are not comparable, and averaging them
    -- produces a number describing no judge that ever existed. Grouping by this
    -- is what makes "did that prompt change help?" answerable at all.
    rules_hash      text        not null,

    -- Soft link to the judge call in model_calls, to join exact cost. Not a
    -- foreign key, for migration 003's reason: telemetry recording is best-effort
    -- (_safe_record can swallow a write), so a hard FK could fail an audit insert
    -- over a call row that never landed. Join where both exist.
    call_key        text,

    created_at      timestamptz not null default now()
);

create index if not exists judge_evals_model_rules_idx
    on judge_evals (model, rules_hash);
create index if not exists judge_evals_case_idx
    on judge_evals (case_name);
create index if not exists judge_evals_created_at_idx
    on judge_evals (created_at desc);

-- The judge's report card. Split by model and by prompt version, so the routing
-- question ("should the judge run on nano?") is answered the way fixer_reliability
-- answered it for the fixer: from the table, not from anecdote. Split by
-- provenance too, so the constructed half never hides inside the real half.
--
-- Deliberately no rate column, unlike fixer_reliability. At eight cases a
-- percentage invites exactly the over-reading this project forbids: one flipped
-- verdict moves an eight-case rate by twelve points and it will still read as a
-- measurement. The counts are here with `graded` next to them, so anyone can
-- divide while looking at what they are dividing. What would settle it: enough
-- cases that a single flip cannot move the number by more than a point. Adding
-- the column then is a one-line migration.
drop view if exists judge_accuracy;
create view judge_accuracy as
select
    model,
    rules_hash,
    provenance,
    count(*)                                                    as graded,
    count(*) filter (where actual = expected)                   as agree,
    count(*) filter (
        where expected = 'not_satisfied' and actual = 'satisfied'
    )                                                           as false_pass,
    count(*) filter (
        where expected = 'satisfied' and actual = 'not_satisfied'
    )                                                           as false_block,
    count(*) filter (where actual = 'uncertain')                as uncertain,
    count(distinct case_name)                                   as cases,
    max(sample) + 1                                             as samples
from judge_evals
group by model, rules_hash, provenance
order by model, provenance;

-- Stability, per criterion. A judge that answers the same input differently
-- across samples is a different failure from one that is consistently wrong, and
-- only one of the two responds to prompt work. This makes the difference visible
-- instead of averaging it away: any row with more than one distinct answer is a
-- criterion the judge cannot make its mind up about.
drop view if exists judge_stability;
create view judge_stability as
select
    model,
    rules_hash,
    case_name,
    criterion_index,
    count(*)                    as samples,
    count(distinct actual)      as distinct_answers,
    min(expected)               as expected,
    string_agg(distinct actual, ', ' order by actual) as answers
from judge_evals
group by model, rules_hash, case_name, criterion_index
having count(*) > 1
order by count(distinct actual) desc, case_name, criterion_index;
