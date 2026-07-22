"""The audit trail for review and fix.

Layer 5 Stage 3, the measurement. Stages 1 and 2 do the work; this records what
the work found and what happened to it, so "is the reviewer worth its cost?" and
"which model is the reliable-enough-cheapest fixer?" stop being anecdotes and
become queries against review_findings (see migration 003).

Shaped like the seam's CallStore, and for the same reason. A store with two
implementations is where this project was burned once: the in-memory double kept
content the Postgres store dropped, and fifteen tests passed while production
broke. So FindingStore is a port with one contract, tested against every
implementation in tests/test_finding_store_contract.py, not per-store.

Recording is append-only audit, not a replay cache, so it carries none of
CallStore's idempotency machinery. It shares one rule with the seam: it must never
fail a run. The recorders swallow store errors the way MeteredClient._safe_record
does, because a meter that can take down the thing it measures is worse than none.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from agentpipe.review import SEVERITIES

# The outcomes a finding can reach. 'reported' is an advisory --review finding that
# nobody acted on; the rest are the fix loop's verdicts.
OUTCOMES: tuple[str, ...] = ("reported", "fixed", "reverted", "unfixable")


@dataclass(frozen=True)
class FindingRow:
    """One audit row. Validated at construction, so an invalid row cannot exist.

    Only the fields we set: id and created_at are server-side defaults, so they are
    not part of the row we write or the row we compare on read-back.
    """

    run_id: str
    round: int
    severity: str
    file: str
    issue: str
    outcome: str
    model: str
    line: Optional[int] = None
    task_ref: Optional[str] = None
    call_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"unknown severity {self.severity!r}; expected one of "
                f"{', '.join(SEVERITIES)}"
            )
        if self.outcome not in OUTCOMES:
            raise ValueError(
                f"unknown outcome {self.outcome!r}; expected one of "
                f"{', '.join(OUTCOMES)}"
            )
        if not self.file.strip():
            raise ValueError("finding row has no file")
        if not self.issue.strip():
            raise ValueError("finding row has no issue")
        if not self.model.strip():
            raise ValueError("finding row has no model")


class FindingStore(ABC):
    """The contract every findings store must honour.

    The rule, in the CallStore spirit: for_run(run_id) returns every row that was
    recorded for that run, equal in every field a caller set. A store that cannot
    promise that is not a store, and the contract test checks the promise against
    all of them.
    """

    @abstractmethod
    def record(self, row: FindingRow) -> None:
        """Append one finding. Append-only; duplicates are not deduplicated."""

    @abstractmethod
    def for_run(self, run_id: str) -> tuple[FindingRow, ...]:
        """Every finding recorded for a run, in the order it was recorded."""


class InMemoryFindingStore(FindingStore):
    """For tests, and the first hour before Postgres is wired."""

    def __init__(self) -> None:
        self.rows: list[FindingRow] = []

    def record(self, row: FindingRow) -> None:
        self.rows.append(row)

    def for_run(self, run_id: str) -> tuple[FindingRow, ...]:
        return tuple(r for r in self.rows if r.run_id == run_id)


class PostgresFindingStore(FindingStore):
    """Supabase / Postgres. Requires migration 003 to have been applied."""

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg  # lazy, so tests need no driver

        self._psycopg = psycopg
        self._dsn = dsn or os.environ["AGENTPIPE_DSN"]

    # One column list and one row mapping used by read and write, so the two cannot
    # disagree. That divergence is the exact bug this port pattern exists to stop.
    _COLS = (
        "run_id, task_ref, round, severity, file, line, issue, outcome, model, "
        "call_key"
    )

    @staticmethod
    def _to_row(row) -> FindingRow:
        return FindingRow(
            run_id=str(row[0]),
            task_ref=row[1],
            round=row[2],
            severity=row[3],
            file=row[4],
            line=row[5],
            issue=row[6],
            outcome=row[7],
            model=row[8],
            call_key=row[9],
        )

    def record(self, row: FindingRow) -> None:
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(
                f"insert into review_findings ({self._COLS}) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.run_id, row.task_ref, row.round, row.severity, row.file,
                    row.line, row.issue, row.outcome, row.model, row.call_key,
                ),
            )

    def for_run(self, run_id: str) -> tuple[FindingRow, ...]:
        with self._psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                f"select {self._COLS} from review_findings "
                "where run_id = %s order by round, created_at",
                (run_id,),
            ).fetchall()
        return tuple(self._to_row(r) for r in rows)


# ---------------------------------------------------------------------------
# Recorders: turn a returned result into audit rows. Never raise.
# ---------------------------------------------------------------------------

def _safe(store: FindingStore, row: FindingRow) -> None:
    """Record one row, swallowing any store error.

    The audit must never be the reason a run fails. Same contract as the seam's
    _safe_record: a diagnostic that can take down the thing it observes is worse
    than no diagnostic.
    """
    try:
        store.record(row)
    except Exception as exc:  # noqa: BLE001
        print(f"[agentpipe] WARN: failed to record finding: {exc}")


def record_review_findings(store: FindingStore, review_result) -> None:
    """Persist an advisory --review run's findings, outcome='reported'.

    Everything comes from the returned ReviewResult, so the reviewer stays
    database-free. The reviewer model and the review call's key come off the one
    recorded call.
    """
    rec = review_result.record
    for f in review_result.findings:
        _safe(store, FindingRow(
            run_id=rec.run_id, task_ref=rec.task_ref, round=0,
            severity=f.severity, file=f.file, line=f.line, issue=f.issue,
            outcome="reported", model=rec.model, call_key=rec.idempotency_key,
        ))


def record_fix_findings(store: FindingStore, fix_result) -> None:
    """Persist a fix loop's rounds, each with its outcome.

    round index is 1-based and matches the loop's own round numbering: the loop
    appends exactly one RoundResult per executed round, worst finding first. The
    fixer model and the fix call's key come off each round's recorded call.
    """
    for i, rd in enumerate(fix_result.rounds, start=1):
        rec = rd.fix_record
        f = rd.finding
        _safe(store, FindingRow(
            run_id=rec.run_id, task_ref=rec.task_ref, round=i,
            severity=f.severity, file=f.file, line=f.line, issue=f.issue,
            outcome=rd.outcome, model=rec.model, call_key=rec.idempotency_key,
        ))
