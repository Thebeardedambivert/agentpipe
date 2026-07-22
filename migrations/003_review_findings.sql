-- Layer 5 Stage 3: the audit table.
--
-- Stages 1 and 2 gave a reviewer and a fixer and proved them on a handful of
-- runs. But everything we "know" is still anecdote: the reviewer found a bug
-- once; nano botched the format twice while mini fixed it once. This table turns
-- anecdote into data. One row per finding the pipeline acted on or reported, with
-- what happened to it, so the questions this layer exists to answer become
-- queries instead of hopes:
--   - is the reviewer worth its cost?          (findings that led to a real fix,
--                                                against review cost in model_calls)
--   - which model is the reliable-enough fixer? (fixed vs reverted/unfixable by
--                                                model: the nano-vs-mini question)
--   - where should min_severity sit?            (which severities' fixes survive)
--
-- Append-only, like model_calls: it is a log, not a cache, so no unique index and
-- duplicates across runs are expected. Run in the Supabase SQL Editor against an
-- existing database, or let CI apply it to a fresh one. Safe to run twice.

create table if not exists review_findings (
    id          uuid primary key default gen_random_uuid(),
    run_id      uuid        not null,
    task_ref    text,

    -- 0 for an advisory --review finding (nobody acted on it); 1..N for the fix
    -- loop's rounds, worst finding first.
    round       int         not null default 0,

    -- The finding itself, as the reviewer reported it.
    severity    text        not null check (severity in ('critical', 'high', 'medium', 'low')),
    file        text        not null,
    line        int,
    issue       text        not null,

    -- What happened. 'reported' is an advisory review finding with no fixer action;
    -- the rest are the fix loop's outcomes.
    outcome     text        not null check (outcome in ('reported', 'fixed', 'reverted', 'unfixable')),

    -- The model responsible for the outcome: the reviewer model for 'reported', the
    -- fixer model for the rest. Grouping by this answers the routing question.
    model       text        not null,

    -- Soft link to the responsible call in model_calls (the review call for
    -- 'reported', the fix call otherwise), to join exact cost. Not a foreign key:
    -- model_calls has none, and telemetry recording is best-effort (_safe_record
    -- can swallow a write), so a hard FK could fail an audit insert over a call row
    -- that never landed. Join where both exist.
    call_key    text,

    created_at  timestamptz not null default now()
);

create index if not exists review_findings_run_id_idx
    on review_findings (run_id);
create index if not exists review_findings_model_outcome_idx
    on review_findings (model, outcome);
create index if not exists review_findings_created_at_idx
    on review_findings (created_at desc);

-- What the reviewer finds, and what happens to it, by severity.
drop view if exists finding_outcomes;
create view finding_outcomes as
select
    severity,
    outcome,
    count(*) as findings
from review_findings
group by severity, outcome
order by
    array_position(array['critical', 'high', 'medium', 'low'], severity),
    outcome;

-- The nano-vs-mini answer, live. Only the fix loop's outcomes, grouped by the
-- fixer model, with the share that survived validation. 'reported' is excluded
-- because an advisory finding had no fixer and no chance to succeed or fail.
drop view if exists fixer_reliability;
create view fixer_reliability as
select
    model,
    count(*)                                        as attempts,
    count(*) filter (where outcome = 'fixed')       as fixed,
    count(*) filter (where outcome = 'reverted')    as reverted,
    count(*) filter (where outcome = 'unfixable')   as unfixable,
    round(
        100.0 * count(*) filter (where outcome = 'fixed') / nullif(count(*), 0), 1
    )                                               as fix_rate_pct
from review_findings
where outcome in ('fixed', 'reverted', 'unfixable')
group by model
order by attempts desc;
