-- Migration: see the model's output, not just its size.
--
-- Two columns, both about the same blind spot. `output_tokens` counts reasoning
-- and answer together, so a model that thinks for 400 tokens and says nothing
-- looks identical to one that had nothing to say. And nothing recorded *why* a
-- response ended, so "the reply was empty" was a mystery rather than a fact.
--
-- Run this in the Supabase SQL Editor against an existing agentpipe database.
-- Safe to run twice.

alter table model_calls
    add column if not exists reasoning_tokens int not null default 0,
    add column if not exists finish_reason    text;

comment on column model_calls.reasoning_tokens is
    'Subset of output_tokens, not additional to it. Already billed as output.';

comment on column model_calls.finish_reason is
    'stop = model finished. length = cut off mid-thought, output is a fragment.';

-- The view that answers the question this migration exists for.
create or replace view output_shape as
select
    task_ref,
    model,
    finish_reason,
    input_tokens,
    output_tokens,
    reasoning_tokens,
    output_tokens - reasoning_tokens              as answer_tokens,
    round(
        100.0 * reasoning_tokens / nullif(output_tokens, 0), 1
    )                                             as pct_thinking,
    cost_usd,
    created_at
from model_calls
where status = 'ok'
order by created_at desc;

-- Rebuilt to show the gap reasoning opens up.
--
-- ratio uses output_tokens, so reasoning flatters it: 1,500 in and 400 out
-- reads as a healthy 3.8 even when 396 of those were thinking and the reply was
-- four words. answer_ratio uses only the reply, and says 375.
--
-- Read them side by side. A big gap means you are paying for thought, which may
-- be worth it, but you should know you are doing it.
--
-- Dropped first, not replaced. `create or replace view` can only append columns
-- to the end, and this adds avg_thinking and answer_ratio in the middle, which it
-- refuses with "cannot change name of view column". Harmless on Supabase, where
-- this view was built by hand, but fatal on a fresh database applying schema.sql
-- then this file in order. CI is the fresh database that caught it. Still safe to
-- run twice: the drop is guarded, the create rebuilds.
drop view if exists ratio_by_role;
create view ratio_by_role as
select
    role,
    count(*)                                                    as calls,
    round(avg(input_tokens))                                    as avg_input,
    round(avg(output_tokens))                                   as avg_output,
    round(avg(reasoning_tokens))                                as avg_thinking,
    round(avg(input_tokens) / nullif(avg(output_tokens), 0), 1) as ratio,
    round(
        avg(input_tokens)
        / nullif(avg(output_tokens - reasoning_tokens), 0), 1
    )                                                           as answer_ratio,
    round(sum(cost_usd), 4)                                     as total_cost_usd,
    round(
        100.0 * sum(cached_input_tokens)::numeric
        / nullif(sum(input_tokens), 0), 1
    )                                                           as cache_hit_pct
from model_calls
where status = 'ok'
group by role
order by total_cost_usd desc;
