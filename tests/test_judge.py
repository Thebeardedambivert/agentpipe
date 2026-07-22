"""Judge tests.

A fake model returning a canned verdict, so these are free and deterministic and
never call OpenAI. They test the judge's contract: it grades the ticket's check-less
acceptance criteria, returns a per-criterion three-state verdict, passes only when
every criterion is satisfied, spends nothing when there is nothing to judge, and
refuses a malformed or incomplete verdict.

This is Layer 6 Stage 1: the judge reads and reports, it does not gate. Self-
contained fake (the CI lesson: a bare `pytest` has no repo root on sys.path).
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agentpipe.judge import (
    CriterionOutcome,
    CriterionVerdict,
    JudgeError,
    JudgeVerdict,
    parse_verdict,
    run_judge,
)
from agentpipe.repo import Repo
from agentpipe.telemetry import InMemoryCallStore, MeteredClient, PriceMap
from agentpipe.ticket import Ticket

PRICES = PriceMap({"fake": {"input": 1.0, "cached_input": 0.1, "output": 10.0}})


class FakeOpenAI:
    """Returns one canned reply and counts calls, so 'no call was made' is provable."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.reply),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=60,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def client_for(reply: str) -> tuple[MeteredClient, FakeOpenAI]:
    fake = FakeOpenAI(reply)
    return (
        MeteredClient(store=InMemoryCallStore(), prices=PRICES,
                      client=fake, run_id="judge-run"),  # type: ignore[arg-type]
        fake,
    )


def verdict(*entries: dict) -> str:
    return "--- verdict\n" + json.dumps(list(entries)) + "\n--- end"


def _ticket(acceptance: str) -> Ticket:
    return Ticket.parse(
        f"""# TASK-JUDGE

## Goal
truncate(text, length) shortens a string to at most length characters, safely.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
{acceptance}

## Files
- truncate.py
"""
    )


# Two check-less (semantic) criteria: indices 0 and 1.
SEMANTIC = (
    "- [ ] truncate rejects a negative length with a clear error\n"
    "- [ ] the result never exceeds length characters"
)
# One criterion, machine-checked: no semantic criteria for the judge to grade.
ALL_CHECKED = '- [ ] truncate exists `check: python -c "import truncate"`'


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "truncate.py").write_text(
        "def truncate(text, length):\n    return text[:length]\n",
        encoding="utf-8", newline="\n",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


FILES = ("truncate.py",)


# --- the overall verdict ---------------------------------------------------

def test_all_criteria_satisfied_passes(repo):
    reply = verdict(
        {"criterion": 0, "outcome": "satisfied", "reason": "guards negative length"},
        {"criterion": 1, "outcome": "satisfied", "reason": "slice bounds the length"},
    )
    client, _ = client_for(reply)
    r = run_judge(_ticket(SEMANTIC), repo, client, "fake", FILES)
    assert r.verdict is JudgeVerdict.PASS
    assert r.passed


def test_one_not_satisfied_blocks(repo):
    reply = verdict(
        {"criterion": 0, "outcome": "not_satisfied", "reason": "no negative guard"},
        {"criterion": 1, "outcome": "satisfied", "reason": "slice bounds the length"},
    )
    client, _ = client_for(reply)
    r = run_judge(_ticket(SEMANTIC), repo, client, "fake", FILES)
    assert r.verdict is JudgeVerdict.BLOCK
    assert not r.passed
    bad = [v for v in r.verdicts if v.outcome is CriterionOutcome.NOT_SATISFIED]
    assert bad and "negative guard" in bad[0].reason


def test_uncertain_does_not_pass(repo):
    reply = verdict(
        {"criterion": 0, "outcome": "uncertain", "reason": "cannot tell from this code"},
        {"criterion": 1, "outcome": "satisfied", "reason": "slice bounds the length"},
    )
    client, _ = client_for(reply)
    r = run_judge(_ticket(SEMANTIC), repo, client, "fake", FILES)
    assert r.verdict is JudgeVerdict.BLOCK


def test_no_semantic_criteria_is_unguarded_and_free(repo):
    """Every criterion is machine-checked, so the judge has nothing to grade. It must
    say so and spend nothing: no model call at all."""
    client, fake = client_for(verdict())
    r = run_judge(_ticket(ALL_CHECKED), repo, client, "fake", FILES)
    assert r.verdict is JudgeVerdict.UNGUARDED
    assert r.record is None
    assert fake.calls == 0


def test_the_judge_call_is_recorded_as_judge_eval(repo):
    reply = verdict(
        {"criterion": 0, "outcome": "satisfied", "reason": "ok"},
        {"criterion": 1, "outcome": "satisfied", "reason": "ok"},
    )
    client, _ = client_for(reply)
    r = run_judge(_ticket(SEMANTIC), repo, client, "fake", FILES)
    assert r.record.role == "judge"
    assert r.record.attempt_kind == "eval"
    assert r.record.task_ref == "TASK-JUDGE"


def test_verdicts_come_back_in_criterion_order(repo):
    # The model lists criterion 1 before 0; run_judge returns them in index order.
    reply = verdict(
        {"criterion": 1, "outcome": "satisfied", "reason": "b"},
        {"criterion": 0, "outcome": "not_satisfied", "reason": "a"},
    )
    client, _ = client_for(reply)
    r = run_judge(_ticket(SEMANTIC), repo, client, "fake", FILES)
    assert r.verdicts[0].reason == "a"
    assert r.verdicts[1].reason == "b"


# --- the unrepresentable-invalid-state guard on CriterionVerdict -----------

def test_criterionverdict_rejects_a_non_enum_outcome():
    with pytest.raises(ValueError, match="CriterionOutcome"):
        CriterionVerdict("some criterion", "satisfied", "reason")  # type: ignore[arg-type]


def test_criterionverdict_rejects_an_empty_reason():
    with pytest.raises(ValueError, match="no reason"):
        CriterionVerdict("some criterion", CriterionOutcome.SATISFIED, "   ")


# --- refusing what it cannot trust -----------------------------------------

CRITERIA = ("first criterion", "second criterion")


def test_prose_instead_of_a_block_is_refused():
    with pytest.raises(JudgeError, match="no '--- verdict' block"):
        parse_verdict("Looks fine to me.", CRITERIA)


def test_a_non_list_body_is_refused():
    with pytest.raises(JudgeError, match="must be a JSON array"):
        parse_verdict('--- verdict\n{"criterion": 0}\n--- end', CRITERIA)


def test_an_unknown_outcome_is_refused():
    body = json.dumps([
        {"criterion": 0, "outcome": "maybe", "reason": "x"},
        {"criterion": 1, "outcome": "satisfied", "reason": "y"},
    ])
    with pytest.raises(JudgeError, match="unknown outcome"):
        parse_verdict(f"--- verdict\n{body}\n--- end", CRITERIA)


def test_an_out_of_range_index_is_refused():
    body = json.dumps([{"criterion": 5, "outcome": "satisfied", "reason": "x"}])
    with pytest.raises(JudgeError, match="out of range"):
        parse_verdict(f"--- verdict\n{body}\n--- end", CRITERIA)


def test_an_incomplete_verdict_is_refused():
    """Judging only some criteria is not a judgment. It must cover every one."""
    body = json.dumps([{"criterion": 0, "outcome": "satisfied", "reason": "x"}])
    with pytest.raises(JudgeError, match="cover every criterion"):
        parse_verdict(f"--- verdict\n{body}\n--- end", CRITERIA)


def test_a_criterion_judged_twice_is_refused():
    body = json.dumps([
        {"criterion": 0, "outcome": "satisfied", "reason": "x"},
        {"criterion": 0, "outcome": "not_satisfied", "reason": "y"},
    ])
    with pytest.raises(JudgeError, match="judged twice"):
        parse_verdict(f"--- verdict\n{body}\n--- end", CRITERIA)
