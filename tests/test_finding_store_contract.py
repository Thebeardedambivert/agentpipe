"""The contract every FindingStore must honour, checked against every store.

Same shape and same reason as tests/test_store_contract.py. A store with two
implementations is where this project was burned: a test double that quietly
disagreed with the real store passed fifteen tests while production broke. So the
promise (for_run returns what record was given, in every field) is tested once,
against all implementations, not per-store.

And the same second lesson: every row written to a real database here is tagged
CONTRACT-TEST and deleted afterwards, pass or fail, so a diagnostic never dirties
the measurement table it exists to protect.
"""

from __future__ import annotations

import os
import uuid

import pytest

from agentpipe.findings import (
    FindingRow,
    InMemoryFindingStore,
    PostgresFindingStore,
)

TEST_TASK_REF = "CONTRACT-TEST"
TEST_MODEL = "contract-test-model"


def _row(**over) -> FindingRow:
    base = dict(
        run_id=str(uuid.uuid4()),
        round=1,
        severity="high",
        file="truncate.py",
        line=2,
        issue="always appends ellipsis, can exceed the maximum length",
        outcome="fixed",
        model=TEST_MODEL,
        task_ref=TEST_TASK_REF,
        call_key=f"contract-{uuid.uuid4()}",
    )
    base.update(over)
    return FindingRow(**base)  # type: ignore[arg-type]


def _memory() -> InMemoryFindingStore:
    return InMemoryFindingStore()


def _postgres() -> PostgresFindingStore:
    if not os.environ.get("AGENTPIPE_DSN"):
        pytest.skip("AGENTPIPE_DSN not set")
    return PostgresFindingStore()


@pytest.fixture(autouse=True)
def _purge_after():
    """Delete every row this file wrote, pass or fail. autouse so it cannot be
    forgotten, after the yield so it fires even when the test raised."""
    yield
    dsn = os.environ.get("AGENTPIPE_DSN")
    if not dsn:
        return
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "delete from review_findings where task_ref = %s or model = %s",
            (TEST_TASK_REF, TEST_MODEL),
        )


stores = pytest.mark.parametrize(
    "make_store", [_memory, _postgres], ids=["memory", "postgres"]
)


def test_the_fixture_cannot_be_mistaken_for_real_data():
    row = _row()
    assert row.task_ref == "CONTRACT-TEST"
    assert "test" in row.model


@stores
def test_a_row_round_trips_every_field(make_store):
    store = make_store()
    row = _row()
    store.record(row)
    got = store.for_run(row.run_id)
    assert got == (row,)  # frozen dataclass: equal in every field we set


@stores
def test_a_null_line_round_trips(make_store):
    store = make_store()
    row = _row(line=None, issue="whole file has no error handling")
    store.record(row)
    assert store.for_run(row.run_id)[0].line is None


@stores
def test_all_rows_for_a_run_come_back_in_order(make_store):
    store = make_store()
    run = str(uuid.uuid4())
    store.record(_row(run_id=run, round=1, outcome="fixed"))
    store.record(_row(run_id=run, round=2, outcome="reverted"))
    got = store.for_run(run)
    assert [r.round for r in got] == [1, 2]
    assert [r.outcome for r in got] == ["fixed", "reverted"]


@stores
def test_for_run_is_empty_for_an_unknown_run(make_store):
    store = make_store()
    assert store.for_run(str(uuid.uuid4())) == ()
