"""Reviewer tests.

A fake model returning a canned reviewer reply, so these are free and
deterministic and never call OpenAI. They test the reviewer's contract: it
parses only well-formed findings, refuses everything else, ranks worst-first,
filters by severity, and records its call through the one door as a reviewer.

This is Layer 5 Stage 1: the reviewer reads, it does not write. There is no
fixer and no loop to test here yet; both are Stage 2.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agentpipe.repo import Repo
from agentpipe.review import (
    Finding,
    ReviewError,
    parse_findings,
    run_review,
)
from agentpipe.telemetry import InMemoryCallStore, MeteredClient, PriceMap
from agentpipe.ticket import Ticket
from tests.test_loop import SequencedFakeOpenAI

PRICES = PriceMap({"fake": {"input": 1.0, "cached_input": 0.1, "output": 10.0}})


def findings_reply(*findings: dict) -> str:
    """A reviewer reply in the format REVIEW_RULES asks for."""
    return "--- findings\n" + json.dumps(list(findings)) + "\n--- end"


def client_for(reply: str) -> MeteredClient:
    fake = SequencedFakeOpenAI([reply])
    return MeteredClient(
        store=InMemoryCallStore(), prices=PRICES,
        client=fake, run_id="review-run",  # type: ignore[arg-type]
    )


def _ticket() -> Ticket:
    return Ticket.parse(
        """# TASK-REVIEW

## Goal
truncate.py should shorten a string to a maximum length without crashing.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] truncate exists and is safe

## Files
- truncate.py
"""
    )


@pytest.fixture
def repo(tmp_path):
    # A real file on disk, because the reviewer reads current contents.
    (tmp_path / "truncate.py").write_text(
        "def truncate(text, length):\n    return text[:length]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


# --- the happy paths -------------------------------------------------------

def test_clean_code_yields_no_findings(repo):
    """An empty array is a valid review: 'I looked and it is fine.'"""
    client = client_for(findings_reply())  # []
    r = run_review(_ticket(), repo, client, "fake", files=("truncate.py",))
    assert r.clean
    assert r.findings == ()


def test_findings_are_ranked_worst_first(repo):
    """The model lists them in any order; we rank by severity."""
    client = client_for(findings_reply(
        {"severity": "low", "file": "truncate.py", "line": 2, "issue": "nit"},
        {"severity": "critical", "file": "truncate.py", "line": 1, "issue": "boom"},
        {"severity": "medium", "file": "truncate.py", "line": None, "issue": "meh"},
    ))
    r = run_review(_ticket(), repo, client, "fake", files=("truncate.py",))
    assert [f.severity for f in r.findings] == ["critical", "medium", "low"]


def test_min_severity_filters_out_the_low_ones(repo):
    client = client_for(findings_reply(
        {"severity": "high", "file": "truncate.py", "line": 1, "issue": "real"},
        {"severity": "low", "file": "truncate.py", "line": 2, "issue": "nit"},
    ))
    r = run_review(_ticket(), repo, client, "fake",
                   files=("truncate.py",), min_severity="high")
    assert [f.severity for f in r.findings] == ["high"]


def test_the_reviewer_call_is_recorded_as_a_reviewer(repo):
    """Stage 3's audit joins on this. The call must land as role=reviewer,
    attempt_kind=review, so its cost is attributable."""
    client = client_for(findings_reply(
        {"severity": "high", "file": "truncate.py", "line": 1, "issue": "x"},
    ))
    r = run_review(_ticket(), repo, client, "fake", files=("truncate.py",))
    assert r.record.role == "reviewer"
    assert r.record.attempt_kind == "review"
    assert r.record.task_ref == "TASK-REVIEW"


# --- refusing what it cannot trust -----------------------------------------

def test_prose_instead_of_a_block_is_refused(repo):
    client = client_for("Looks good to me, ship it!")
    with pytest.raises(ReviewError, match="no '--- findings' block"):
        run_review(_ticket(), repo, client, "fake", files=("truncate.py",))


def test_empty_reviewer_reply_is_refused_not_treated_as_clean(repo):
    """A reviewer that says nothing is a failure, not a clean bill of health.

    The seam records an empty reply as an error; run_review must surface that as
    a ReviewError rather than returning a falsely-clean result. This is the
    project's signature bug (no exception is not success) guarded at the reviewer.
    """
    client = client_for("")
    with pytest.raises(ReviewError):
        run_review(_ticket(), repo, client, "fake", files=("truncate.py",))


def test_body_that_is_not_a_list_is_refused():
    with pytest.raises(ReviewError, match="must be a JSON array"):
        parse_findings('--- findings\n{"severity": "low"}\n--- end')


def test_invalid_json_is_refused():
    with pytest.raises(ReviewError, match="not valid JSON"):
        parse_findings("--- findings\n[not json]\n--- end")


def test_a_finding_missing_a_field_is_refused():
    with pytest.raises(ReviewError, match="missing required field"):
        parse_findings('--- findings\n[{"severity": "low", "file": "a.py"}]\n--- end')


def test_an_unknown_severity_is_refused():
    with pytest.raises(ReviewError, match="invalid"):
        parse_findings(
            '--- findings\n[{"severity": "urgent", "file": "a.py", "issue": "x"}]\n--- end'
        )


# --- the unrepresentable-invalid-state guard on Finding --------------------

def test_finding_rejects_unknown_severity():
    with pytest.raises(ValueError, match="unknown severity"):
        Finding(severity="urgent", file="a.py", issue="x")


def test_finding_rejects_empty_issue():
    with pytest.raises(ValueError, match="no issue"):
        Finding(severity="low", file="a.py", issue="   ")


def test_finding_rejects_a_zero_or_bool_line():
    with pytest.raises(ValueError, match="positive integer"):
        Finding(severity="low", file="a.py", issue="x", line=0)
    with pytest.raises(ValueError, match="positive integer"):
        Finding(severity="low", file="a.py", issue="x", line=True)


def test_finding_allows_a_null_line():
    f = Finding(severity="low", file="a.py", issue="x", line=None)
    assert f.line is None
