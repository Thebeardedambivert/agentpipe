"""The contract every CallStore must honour, checked against every store.

This file exists because of a bug that cost a whole debugging session.

InMemoryCallStore kept the entire CallRecord in a dict, content included.
PostgresCallStore recorded every column except content and returned "" on
replay. Fifteen idempotency tests passed, because they all used the in-memory
store. In production, every replayed call handed an empty string to the parser,
which failed with "empty reply", which looked like a model problem and was not.

The test double and the real thing disagreed about the one property that
mattered, and nothing anywhere checked that they agreed.

So: one set of tests, parameterised over every implementation. A store that
cannot pass these is not a store. If a third one is ever added, it gets added to
`stores` below and has to earn its place like the others.

The Postgres tests skip when AGENTPIPE_DSN is unset, which means they will not
run in a bare CI job. That is a real hole and it is the same hole that caused the
bug: a promise nobody checks. If this project ever gets CI, it needs a Postgres
service container, not a skip.

And a second lesson, learned the same afternoon: the first version of this file
wrote fixture rows into whatever AGENTPIPE_DSN pointed at, which was a real
database, tagged task_ref='TASK-1' like a real run. Six fake rows with invented
token counts went into the table this project exists to keep honest, and
ratio_by_role started averaging them in with real calls. The tests for the meter
corrupted the meter.

So: every row written here is tagged CONTRACT-TEST, and every row is deleted
afterwards whether the test passed or not. Tests that touch a real store clean up
after themselves. There is no version of "it is only a few rows" that is true
when the rows are your measurements.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

from agentpipe.telemetry import (
    CallRecord,
    InMemoryCallStore,
    PostgresCallStore,
    Usage,
)


# Tagged so that anything leaking into a real table is obvious, greppable, and
# deletable. Never reuse a task_ref that a real ticket could have.
TEST_TASK_REF = "CONTRACT-TEST"
TEST_MODEL = "contract-test-model"


def _record(**over) -> CallRecord:
    base = dict(
        run_id=str(uuid.uuid4()),
        idempotency_key=f"contract-{uuid.uuid4()}",
        role="builder",
        attempt_kind="implement",
        attempt_index=1,
        model=TEST_MODEL,
        usage=Usage(
            input_tokens=1733,
            output_tokens=190,
            cached_input_tokens=64,
            reasoning_tokens=12,
        ),
        cost_usd=Decimal("0.002155"),
        status="ok",
        duration_ms=4359,
        task_ref=TEST_TASK_REF,
        pack_hash="contract-test-pack",
        finish_reason="stop",
        content="--- prices.example.json\n{}\n--- end",
    )
    base.update(over)
    return CallRecord(**base)  # type: ignore[arg-type]


def _memory() -> InMemoryCallStore:
    return InMemoryCallStore()


def _postgres() -> PostgresCallStore:
    if not os.environ.get("AGENTPIPE_DSN"):
        pytest.skip("AGENTPIPE_DSN not set")
    return PostgresCallStore()


@pytest.fixture(autouse=True)
def _purge_after():
    """Delete every row this file wrote, pass or fail.

    autouse so it cannot be forgotten on a new test. Runs after the yield so it
    fires even when the test raised, which is exactly when a half-written row is
    most likely to be left behind.
    """
    yield
    dsn = os.environ.get("AGENTPIPE_DSN")
    if not dsn:
        return
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "delete from model_calls where task_ref = %s or model = %s",
            (TEST_TASK_REF, TEST_MODEL),
        )


stores = pytest.mark.parametrize(
    "make_store", [_memory, _postgres], ids=["memory", "postgres"]
)


def test_the_fixture_cannot_be_mistaken_for_real_data():
    """Guard on the guard.

    If someone ever changes these back to something realistic, this fails. A
    test row that looks like a real row is a measurement you will trust and
    should not.
    """
    rec = _record()
    assert rec.task_ref == "CONTRACT-TEST"
    assert "test" in rec.model
    assert rec.idempotency_key.startswith("contract-")


@stores
def test_content_round_trips(make_store):
    """The bug, as a test.

    If this had existed, the postgres store would have failed it on day one and
    the debugging session would have been ten seconds long.
    """
    store = make_store()
    rec = _record()
    store.record(rec)
    got = store.find(rec.idempotency_key)
    assert got is not None
    assert got.content == rec.content


@stores
def test_usage_round_trips(make_store):
    store = make_store()
    rec = _record()
    store.record(rec)
    got = store.find(rec.idempotency_key)
    assert got.usage.input_tokens == 1733
    assert got.usage.output_tokens == 190
    assert got.usage.cached_input_tokens == 64
    assert got.usage.reasoning_tokens == 12


@stores
def test_cost_round_trips_exactly(make_store):
    """Decimal in, Decimal out, same value. It is money."""
    store = make_store()
    rec = _record()
    store.record(rec)
    assert store.find(rec.idempotency_key).cost_usd == Decimal("0.002155")


@stores
def test_metadata_round_trips(make_store):
    store = make_store()
    rec = _record()
    store.record(rec)
    got = store.find(rec.idempotency_key)
    assert got.finish_reason == "stop"
    assert got.pack_hash == "contract-test-pack"
    assert got.task_ref == TEST_TASK_REF
    assert got.role == "builder"
    assert got.attempt_index == 1
    assert got.status == "ok"


@stores
def test_missing_key_returns_none(make_store):
    assert make_store().find(f"never-{uuid.uuid4()}") is None


@stores
def test_recording_twice_keeps_the_first(make_store):
    """Atomic on idempotency_key. Second write is a no-op, not an overwrite."""
    store = make_store()
    rec = _record(content="first")
    store.record(rec)
    store.record(_record(idempotency_key=rec.idempotency_key, content="second"))
    assert store.find(rec.idempotency_key).content == "first"


@stores
def test_errors_round_trip(make_store):
    store = make_store()
    rec = _record(status="error", error="provider exploded", content="")
    store.record(rec)
    got = store.find(rec.idempotency_key)
    assert got.status == "error"
    assert got.error == "provider exploded"


@stores
def test_latest_for_run_returns_the_highest_attempt_with_its_content(make_store):
    """What makes a crashed run resumable.

    The latest attempt's index says where to continue, and its stored content is
    what we re-apply to recover work that was recorded but never reached disk. So
    this must return the highest-numbered attempt for the run, content included.
    """
    store = make_store()
    run = str(uuid.uuid4())
    store.record(_record(run_id=run, attempt_index=1, content="first reply"))
    store.record(_record(run_id=run, attempt_index=2, content="second reply"))

    got = store.latest_for_run(run)
    assert got is not None
    assert got.attempt_index == 2
    assert got.content == "second reply"


@stores
def test_latest_for_run_is_none_for_an_unknown_run(make_store):
    # A real, well-formed run id that simply was never recorded. run_id is a uuid
    # column, so the value has to be a valid uuid, not arbitrary text.
    assert make_store().latest_for_run(str(uuid.uuid4())) is None
