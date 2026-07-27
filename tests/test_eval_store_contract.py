"""The contract every EvalStore must honour, checked against every store.

Same shape and same reason as tests/test_store_contract.py and
tests/test_finding_store_contract.py. A store with two implementations is where
this project was burned: a test double that quietly disagreed with the real store
passed fifteen tests while production broke. So the promise (for_run returns what
record was given, in every field) is tested once, against all implementations, not
per-store.

And the same second lesson, which matters more here than anywhere else: every row
written to a real database is tagged CONTRACT-TEST and deleted afterwards, pass or
fail. judge_evals is evidence about evidence. A stray fixture row does not just
skew a cost average; it tells you the judge was wrong about a case that never
existed.
"""

from __future__ import annotations

import os
import uuid

import pytest

from agentpipe.evalstore import EvalRow, InMemoryEvalStore, PostgresEvalStore

TEST_CASE_NAME = "CONTRACT-TEST"
TEST_MODEL = "contract-test-model"


def _row(**over) -> EvalRow:
    base = dict(
        run_id=str(uuid.uuid4()),
        case_name=TEST_CASE_NAME,
        provenance="constructed",
        source=None,
        criterion_index=0,
        criterion="truncate rejects a negative length with a clear error",
        expected="not_satisfied",
        actual="satisfied",
        sample=0,
        model=TEST_MODEL,
        rules_hash="contracttesthash",
        call_key=f"contract-{uuid.uuid4()}",
    )
    base.update(over)
    return EvalRow(**base)  # type: ignore[arg-type]


def _memory() -> InMemoryEvalStore:
    return InMemoryEvalStore()


def _postgres() -> PostgresEvalStore:
    if not os.environ.get("AGENTPIPE_DSN"):
        pytest.skip("AGENTPIPE_DSN not set")
    return PostgresEvalStore()


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
            "delete from judge_evals where case_name = %s or model = %s",
            (TEST_CASE_NAME, TEST_MODEL),
        )


stores = pytest.mark.parametrize(
    "make_store", [_memory, _postgres], ids=["memory", "postgres"]
)


def test_the_fixture_cannot_be_mistaken_for_real_data():
    row = _row()
    assert row.case_name == "CONTRACT-TEST"
    assert "test" in row.model


@stores
def test_a_row_round_trips_every_field(make_store):
    store = make_store()
    row = _row()
    store.record(row)
    got = store.for_run(row.run_id)
    assert got == (row,)  # frozen dataclass: equal in every field we set


@stores
def test_a_null_source_round_trips(make_store):
    """Constructed cases have no source. A store that turns None into '' would
    make every constructed case look harvested."""
    store = make_store()
    row = _row(source=None)
    store.record(row)
    assert store.for_run(row.run_id)[0].source is None


@stores
def test_a_real_case_keeps_its_source(make_store):
    store = make_store()
    row = _row(provenance="real", source="TASK-JUDGE-THIN")
    store.record(row)
    got = store.for_run(row.run_id)[0]
    assert got.provenance == "real"
    assert got.source == "TASK-JUDGE-THIN"


@stores
def test_every_sample_of_every_criterion_comes_back(make_store):
    """Repeats are the whole point of the sample column: four rows, not one."""
    store = make_store()
    run = str(uuid.uuid4())
    for sample in (0, 1):
        for idx in (0, 1):
            store.record(_row(run_id=run, sample=sample, criterion_index=idx))
    got = store.for_run(run)
    assert len(got) == 4
    assert sorted((r.sample, r.criterion_index) for r in got) == [
        (0, 0), (0, 1), (1, 0), (1, 1),
    ]


@stores
def test_the_uncertain_answer_survives_the_round_trip(make_store):
    """Three-state in, three-state out.

    A store that collapsed 'uncertain' into 'not_satisfied' would erase the
    distinction judge.py exists to draw, and the loss would look like an ordinary
    disagreement in every report built on it.
    """
    store = make_store()
    row = _row(actual="uncertain")
    store.record(row)
    assert store.for_run(row.run_id)[0].actual == "uncertain"


@stores
def test_for_run_is_empty_for_an_unknown_run(make_store):
    store = make_store()
    assert store.for_run(str(uuid.uuid4())) == ()


def test_a_label_of_uncertain_cannot_be_stored():
    """The two-state label rule, enforced at construction and in the schema.

    'uncertain' is the judge's answer, never the ground truth it is measured
    against. A row that recorded it as expected would be scoring the judge against
    a shrug.
    """
    with pytest.raises(ValueError, match="not ready"):
        _row(expected="uncertain")


def test_a_row_without_a_rules_hash_cannot_be_stored():
    """An accuracy row that cannot say which prompt produced it is not comparable
    to any other row, which makes it worse than absent."""
    with pytest.raises(ValueError, match="rules_hash"):
        _row(rules_hash="")


def test_an_unknown_provenance_cannot_be_stored():
    with pytest.raises(ValueError, match="unknown provenance"):
        _row(provenance="borrowed")
