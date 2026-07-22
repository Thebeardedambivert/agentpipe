"""Recorder tests: turning a returned result into audit rows.

Self-contained (own fakes, no cross-test import), free, deterministic. The
recorders read only attributes off the results, so lightweight namespaces stand in
for a full ReviewResult / ReviewFixResult. Real Finding objects are used, because
their validation is part of the contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentpipe.findings import (
    FindingRow,
    InMemoryFindingStore,
    record_fix_findings,
    record_review_findings,
)
from agentpipe.review import Finding


def _call(model: str, run_id: str = "run-1", key: str = "key-1", task: str = "TASK-X"):
    """A stand-in for the CallRecord the recorders read fields off."""
    return SimpleNamespace(run_id=run_id, model=model, idempotency_key=key, task_ref=task)


# --- advisory review -> outcome 'reported' ---------------------------------

def test_review_findings_are_recorded_as_reported():
    store = InMemoryFindingStore()
    review_result = SimpleNamespace(
        record=_call("reviewer-m", key="rev-key"),
        findings=(
            Finding(severity="high", file="a.py", issue="bug", line=1),
            Finding(severity="low", file="b.py", issue="nit", line=None),
        ),
    )
    record_review_findings(store, review_result)

    rows = store.for_run("run-1")
    assert len(rows) == 2
    assert all(r.outcome == "reported" for r in rows)
    assert all(r.round == 0 for r in rows)          # advisory findings are round 0
    assert all(r.model == "reviewer-m" for r in rows)
    assert all(r.call_key == "rev-key" for r in rows)


# --- fix loop -> one row per round with its outcome ------------------------

def test_fix_findings_record_each_round_with_its_outcome_and_model():
    store = InMemoryFindingStore()
    finding = Finding(severity="high", file="a.py", issue="bug", line=1)
    rounds = [
        SimpleNamespace(finding=finding, outcome="unfixable", fix_record=_call("nano")),
        SimpleNamespace(finding=finding, outcome="fixed", fix_record=_call("mini")),
    ]
    fix_result = SimpleNamespace(rounds=rounds)
    record_fix_findings(store, fix_result)

    rows = store.for_run("run-1")
    assert [r.round for r in rows] == [1, 2]         # 1-based, matches the loop
    assert [r.outcome for r in rows] == ["unfixable", "fixed"]
    assert [r.model for r in rows] == ["nano", "mini"]


# --- the unrepresentable-invalid-state guard on FindingRow -----------------

def test_findingrow_rejects_an_unknown_severity():
    with pytest.raises(ValueError, match="unknown severity"):
        FindingRow(run_id="r", round=0, severity="urgent", file="a.py",
                   issue="x", outcome="reported", model="m")


def test_findingrow_rejects_an_unknown_outcome():
    with pytest.raises(ValueError, match="unknown outcome"):
        FindingRow(run_id="r", round=0, severity="high", file="a.py",
                   issue="x", outcome="maybe", model="m")


def test_findingrow_rejects_an_empty_issue():
    with pytest.raises(ValueError, match="no issue"):
        FindingRow(run_id="r", round=0, severity="high", file="a.py",
                   issue="   ", outcome="reported", model="m")


# --- recording never fails a run -------------------------------------------

def test_recording_swallows_a_failing_store():
    """A store that raises must not propagate: the audit can never fail a run."""
    class Boom(InMemoryFindingStore):
        def record(self, row):
            raise RuntimeError("db is down")

    review_result = SimpleNamespace(
        record=_call("reviewer-m"),
        findings=(Finding(severity="high", file="a.py", issue="bug", line=1),),
    )
    # Must not raise.
    record_review_findings(Boom(), review_result)
