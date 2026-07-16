-- Layer 0: the event store.
--
-- One row per model call. Append-only, never updated, never deleted.
-- This is the substrate Layer 4 (event sourcing) will build on, so we get
-- the append-only discipline right from the start rather than retrofitting it.

create table if not exists model_calls (
    -- Identity
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid        not null,
    idempotency_key text        not null,

    -- Who made the call and why
    role            text        not null check (role in ('builder', 'reviewer', 'fixer', 'judge')),
    attempt_kind    text        not null check (attempt_kind in ('implement', 'validation_retry', 'review_fix', 'review', 'eval')),
    attempt_index   int         not null default 0,
    task_ref        text,

    -- What was sent
    model           text        not null,
    pack_hash       text,

    -- What it cost
    input_tokens        int     not null default 0,
    cached_input_tokens int     not null default 0,
    output_tokens       int     not null default 0,
    cost_usd            numeric(12, 6) not null default 0,

    -- How it went
    status          text        not null check (status in ('ok', 'error', 'replayed')),
    error           text,
    duration_ms     int         not null default 0,

    -- Tracing
    trace_id        text,
    span_id         text,

    created_at      timestamptz not null default now()
);

-- The idempotency guarantee lives here, in the database, not in application code.
-- A1.5's lesson: if the process can die between "call succeeded" and "call
-- recorded", then the only durable claim is one the database enforces.
create unique index if not exists model_calls_idempotency_key_uniq
    on model_calls (idempotency_key);

create index if not exists model_calls_run_id_idx      on model_calls (run_id);
create index if not exists model_calls_created_at_idx  on model_calls (created_at desc);
create index if not exists model_calls_role_idx        on model_calls (role, created_at desc);

-- E5's exercise, answered as a view: what did each run cost, broken down by role?
create or replace view run_costs as
select
    run_id,
    role,
    count(*)                          as calls,
    sum(input_tokens)                 as input_tokens,
    sum(cached_input_tokens)          as cached_input_tokens,
    sum(output_tokens)                as output_tokens,
    round(sum(cost_usd), 4)           as cost_usd,
    round(
        sum(input_tokens)::numeric / nullif(sum(output_tokens), 0),
        1
    )                                 as input_output_ratio,
    min(created_at)                   as started_at,
    max(created_at)                   as ended_at
from model_calls
where status = 'ok'
group by run_id, role;

-- The single number this whole layer exists to produce.
-- Andrew's 70,000 / 100 is an input_output_ratio of 700.
create or replace view ratio_by_role as
select
    role,
    count(*)                                             as calls,
    round(avg(input_tokens))                             as avg_input,
    round(avg(output_tokens))                            as avg_output,
    round(avg(input_tokens) / nullif(avg(output_tokens), 0), 1) as ratio,
    round(sum(cost_usd), 4)                              as total_cost_usd,
    round(
        100.0 * sum(cached_input_tokens)::numeric / nullif(sum(input_tokens), 0),
        1
    )                                                    as cache_hit_pct
from model_calls
where status = 'ok'
group by role
order by total_cost_usd desc;
