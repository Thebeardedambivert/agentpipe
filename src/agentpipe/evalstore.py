"""The audit trail for the judge's own accuracy.

Layer 6 Stage 3, the measurement. Stage 1 built the judge and Stage 2 gave it
command authority over the builder, but nothing in this project knows whether it
is right. This records what the judge said against what a human labelled, one row
per criterion per sample, so "is the gate trustworthy?" and "which model should
judge?" stop being hopes and become queries against judge_evals (migration 004).

Shaped like findings.py, which is shaped like the seam's CallStore, and for the
same reason: a port with two implementations that quietly disagree is the bug this
project was burned by. So EvalStore has one contract, tested against every
implementation in tests/test_eval_store_contract.py, not per-store.

One column deserves explaining. `rules_hash` is the identity of JUDGE_RULES at the
time of the grading. Without it, rows from before and after a prompt edit average
together into a number that describes no judge that ever existed. It is the
difference between "the judge agrees 12 of 14 times" and "some judge, at some
point, agreed about something".

Append-only audit, not a replay cache, so none of CallStore's idempotency
machinery. It shares one rule with the seam and with findings.py: it must never
fail a run. The recorder swallows store errors, because a meter that can take down
the thing it measures is worse than no meter. That matters more here than
anywhere: this table is evidence about evidence.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

# What a human may label a criterion. Deliberately two-state where the judge has
# three: a labeller who is uncertain has not finished making the case. 'uncertain'
# is an answer a judge may give, never a ground truth it can be measured against.
EXPECTED: tuple[str, ...] = ("satisfied", "not_satisfied")

# What the judge may answer. The third state is the whole point of judge.py's
# design, so it has to survive into the audit or the measurement flattens it.
ACTUAL: tuple[str, ...] = ("satisfied", "not_satisfied", "uncertain")

# Where a case came from. Kept as data rather than a naming convention, because
# the report and the view both split on it, and a convention is a check nobody runs.
PROVENANCE: tuple[str, ...] = ("real", "constructed")


@dataclass(frozen=True)
class EvalRow:
    """One graded criterion. Validated at construction, so an invalid row cannot exist.

    id and created_at are server-side defaults, so they are not part of the row we
    write or the row we compare on read-back.
    """

    run_id: str
    case_name: str
    provenance: str
    criterion_index: int
    criterion: str
    expected: str
    actual: str
    model: str
    rules_hash: str
    sample: int = 0
    source: Optional[str] = None
    call_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE:
            raise ValueError(
                f"unknown provenance {self.provenance!r}; expected one of "
                f"{', '.join(PROVENANCE)}"
            )
        if self.expected not in EXPECTED:
            raise ValueError(
                f"unknown expected {self.expected!r}; expected one of "
                f"{', '.join(EXPECTED)}. A label of 'uncertain' is not a label: "
                f"if the ground truth is unclear, the case is not ready."
            )
        if self.actual not in ACTUAL:
            raise ValueError(
                f"unknown actual {self.actual!r}; expected one of {', '.join(ACTUAL)}"
            )
        if not self.case_name.strip():
            raise ValueError("eval row has no case name")
        if not self.criterion.strip():
            raise ValueError("eval row has no criterion text")
        if not self.model.strip():
            raise ValueError("eval row has no model")
        if not self.rules_hash.strip():
            # An accuracy row that cannot say which prompt produced it is not
            # comparable to any other row, which makes it worse than absent.
            raise ValueError("eval row has no rules_hash")
        if self.criterion_index < 0:
            raise ValueError(f"negative criterion index: {self.criterion_index}")
        if self.sample < 0:
            raise ValueError(f"negative sample: {self.sample}")


class EvalStore(ABC):
    """The contract every eval store must honour.

    The rule, in the CallStore spirit: for_run(run_id) returns every row that was
    recorded for that run, equal in every field a caller set. A store that cannot
    promise that is not a store, and the contract test checks the promise against
    all of them.
    """

    @abstractmethod
    def record(self, row: EvalRow) -> None:
        """Append one graded criterion. Append-only; duplicates are not deduplicated."""

    @abstractmethod
    def for_run(self, run_id: str) -> tuple[EvalRow, ...]:
        """Every row recorded for a run, in the order it was recorded."""


class InMemoryEvalStore(EvalStore):
    """For tests, and for reading the numbers before Postgres is wired."""

    def __init__(self) -> None:
        self.rows: list[EvalRow] = []

    def record(self, row: EvalRow) -> None:
        self.rows.append(row)

    def for_run(self, run_id: str) -> tuple[EvalRow, ...]:
        return tuple(r for r in self.rows if r.run_id == run_id)


class PostgresEvalStore(EvalStore):
    """Supabase / Postgres. Requires migration 004 to have been applied."""

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg  # lazy, so tests need no driver

        self._psycopg = psycopg
        self._dsn = dsn or os.environ["AGENTPIPE_DSN"]

    # One column list and one row mapping used by read and write, so the two cannot
    # disagree. That divergence is the exact bug this port pattern exists to stop.
    _COLS = (
        "run_id, case_name, provenance, source, criterion_index, criterion, "
        "expected, actual, sample, model, rules_hash, call_key"
    )

    @staticmethod
    def _to_row(row) -> EvalRow:
        return EvalRow(
            run_id=str(row[0]),
            case_name=row[1],
            provenance=row[2],
            source=row[3],
            criterion_index=row[4],
            criterion=row[5],
            expected=row[6],
            actual=row[7],
            sample=row[8],
            model=row[9],
            rules_hash=row[10],
            call_key=row[11],
        )

    def record(self, row: EvalRow) -> None:
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(
                f"insert into judge_evals ({self._COLS}) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.run_id, row.case_name, row.provenance, row.source,
                    row.criterion_index, row.criterion, row.expected, row.actual,
                    row.sample, row.model, row.rules_hash, row.call_key,
                ),
            )

    def for_run(self, run_id: str) -> tuple[EvalRow, ...]:
        with self._psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                f"select {self._COLS} from judge_evals "
                "where run_id = %s order by case_name, sample, criterion_index",
                (run_id,),
            ).fetchall()
        return tuple(self._to_row(r) for r in rows)


def _safe(store: EvalStore, row: EvalRow) -> None:
    """Record one row, swallowing any store error.

    Same contract as findings._safe and the seam's _safe_record. The audit must
    never be the reason a run fails.
    """
    try:
        store.record(row)
    except Exception as exc:  # noqa: BLE001
        print(f"[agentpipe] WARN: failed to record eval row: {exc}")


def record_eval_scores(store: EvalStore, run_id: str, scores, rules: str) -> None:
    """Persist a graded run: one row per criterion per sample.

    Everything comes from the returned scores, so evals.py stays database-free the
    way review.py does. `rules` is the JUDGE_RULES hash in force for this run,
    passed in rather than read here so the value recorded is the value that was
    actually used, not whatever the module says now.
    """
    for s in scores:
        _safe(store, EvalRow(
            run_id=run_id,
            case_name=s.case_name,
            provenance=s.provenance,
            source=s.source,
            criterion_index=s.criterion_index,
            criterion=s.criterion,
            expected=s.expected.value,
            actual=s.actual.value,
            sample=s.sample,
            model=s.model,
            rules_hash=rules,
            call_key=s.call_key,
        ))
