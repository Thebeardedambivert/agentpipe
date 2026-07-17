-- Migration: keep the answer, not just the receipt.
--
-- The bug this fixes: PostgresCallStore recorded everything about a call except
-- the thing the caller wanted. On replay it returned content="". The parser saw
-- an empty reply and failed. So the feature that exists to save money was
-- silently breaking every second run.
--
-- An idempotency cache that does not store the result is not a cache. It is a
-- log of what happened, wearing a cache's clothes.
--
-- Run alone in the Supabase SQL Editor. Safe to run twice.

alter table model_calls
    add column if not exists content text not null default '';

comment on column model_calls.content is
    'The model reply. Load-bearing: replay returns this. Without it a replayed '
    'call hands back an empty string and every consumer downstream fails.';
