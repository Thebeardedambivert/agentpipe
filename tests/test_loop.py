"""Loop tests.

A fake model returning a *sequence* of replies, so these are free and
deterministic. They test the loop's wiring: that it retries on failure with the
failure in hand, stops for the right reasons, and tells apart "the tests failed"
from "the test runner is broken".

Validation here is a real `python -c "..."` command whose pass/fail depends on
what the fake patch wrote to disk, so the build -> validate -> retry cycle runs
for real without ever calling a model. Commands are portable (no bash-isms) so
they behave the same on the Windows dev machine and Linux CI.
"""

from __future__ import annotations

import subprocess
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agentpipe.loop import run_loop
from agentpipe.repo import Repo
from agentpipe.telemetry import (
    CallRecord,
    InMemoryCallStore,
    MeteredClient,
    PriceMap,
    Usage,
)
from agentpipe.ticket import Ticket

PRICES = PriceMap({"fake": {"input": 1.0, "cached_input": 0.1, "output": 10.0}})

# Validation that passes only when answer.txt contains exactly "42".
VALIDATE_IS_42 = (
    'python -c "import sys, os; sys.exit(0 if os.path.exists(\'answer.txt\') '
    "and open('answer.txt').read().strip() == '42' else 1)\""
)
# A validation command that cannot give an answer: exit 2 is "broken", not "fail".
VALIDATE_BROKEN = 'python -c "import sys; sys.exit(2)"'

# Model replies, in the file-block format RULES asks for.
PATCH_42 = "--- answer.txt\n42\n--- end"
PATCH_7 = "--- answer.txt\n7\n--- end"
PROSE = "Sure, I can help you with that!"


class SequencedFakeOpenAI:
    """Returns replies in order, repeating the last one if calls outrun them."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.messages: list = []  # the messages of every call, in order

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        self.messages.append(kwargs["messages"])
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=reply),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=1500,
                completion_tokens=120,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def client_for(replies: list[str]) -> tuple[MeteredClient, SequencedFakeOpenAI]:
    fake = SequencedFakeOpenAI(replies)
    return (
        MeteredClient(
            store=InMemoryCallStore(), prices=PRICES,
            client=fake, run_id="loop-run",  # type: ignore[arg-type]
        ),
        fake,
    )


def _ticket(validation: str, check: str = "") -> Ticket:
    accept = "- [ ] answer.txt has the right content"
    if check:
        accept += f" `check: {check}`"
    return Ticket.parse(
        f"""# TASK-LOOP

## Goal
answer.txt should contain the exact number the ticket asks for, nothing else.

## Validation
```
{validation}
```

## Acceptance
{accept}

## Files
- answer.txt
"""
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


def test_passes_on_first_try(repo):
    client, fake = client_for([PATCH_42])
    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake", max_attempts=3)
    assert r.verdict == "pass"
    assert r.attempts == 1
    assert fake.calls == 1


def test_fails_then_passes_and_feeds_the_failure_back(repo):
    """The heart of Layer 3: attempt 2 is built with attempt 1's failure in hand."""
    client, fake = client_for([PATCH_7, PATCH_42])
    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake", max_attempts=3)
    assert r.verdict == "pass"
    assert r.attempts == 2
    # The second call is a retry, not a fresh implement.
    assert r.results[1].record.attempt_kind == "validation_retry"
    # And the failure actually reached the model on that retry.
    second_call_user_msg = fake.messages[1][1]["content"]
    assert "The last attempt failed validation" in second_call_user_msg


def test_exhausts_when_never_fixed(repo):
    client, fake = client_for([PATCH_7])  # always wrong
    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake", max_attempts=3)
    assert r.verdict == "exhausted"
    assert r.attempts == 3
    assert fake.calls == 3


def test_broken_validation_stops_loudly_without_burning_the_budget(repo):
    """Exit 2 is a broken command, not a failing test. The model can't fix pytest
    not being installed, so retrying would just waste money."""
    client, fake = client_for([PATCH_42])
    r = run_loop(_ticket(VALIDATE_BROKEN), repo, client, "fake", max_attempts=3)
    assert r.verdict == "blocked"
    assert r.attempts == 1
    assert fake.calls == 1


def test_unparseable_reply_is_a_recoverable_attempt(repo):
    """A prose reply is a failed attempt the model can fix, not a crash."""
    client, fake = client_for([PROSE, PATCH_42])
    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake", max_attempts=3)
    assert r.verdict == "pass"
    assert r.attempts == 2
    assert fake.calls == 2


def test_exhaustion_does_not_trip_the_recursion_limit(repo):
    """A longer run must stop on our 'exhausted' verdict, not LangGraph's
    GraphRecursionError surfacing from underneath."""
    client, _ = client_for([PATCH_7])
    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake", max_attempts=5)
    assert r.verdict == "exhausted"
    assert r.attempts == 5


def test_pack_is_deterministic_across_rebuilds(repo):
    """The loop's idempotency rests on identical repo state producing an
    identical pack. Guard it, in case validation ever perturbs the tree."""
    from agentpipe.pack import build
    from agentpipe.repo import select

    t = _ticket(VALIDATE_IS_42)
    selected = select(t, repo)
    assert build(t, repo, selected).hash == build(t, repo, selected).hash


def test_green_validation_but_failing_acceptance_warns(repo):
    """Validation passing is not proof the ticket's work was done.

    Here validation only checks the file exists, but the acceptance check demands
    content '999'. The patch writes '42': tests go green, acceptance does not, and
    the loop passes while warning that the two disagree.
    """
    validate_exists = (
        'python -c "import os, sys; sys.exit(0 if os.path.exists(\'answer.txt\') else 1)"'
    )
    check_999 = (
        'python -c "import sys; sys.exit(0 if open(\'answer.txt\').read().strip() == \'999\' else 1)"'
    )
    t = _ticket(validate_exists, check=check_999)
    client, _ = client_for([PATCH_42])
    r = run_loop(t, repo, client, "fake", max_attempts=2)
    assert r.verdict == "pass"
    assert r.acceptance_warning is not None
    assert "acceptance" in r.acceptance_warning


# --- resume: continuing a crashed run -------------------------------------

def _recorded(run_id: str, attempt_index: int, content: str, status: str = "ok"):
    """A CallRecord as if a prior attempt had been recorded before a crash."""
    return CallRecord(
        run_id=run_id, idempotency_key=f"k-{uuid.uuid4()}", role="builder",
        attempt_kind="implement", attempt_index=attempt_index, model="fake",
        usage=Usage(input_tokens=1500, output_tokens=120),
        cost_usd=Decimal("0.0027"), status=status, duration_ms=10,
        task_ref="TASK-LOOP", pack_hash="ph", content=content,
    )


def test_resume_recovers_landed_work_for_free(repo):
    """The crash happened after the fix was recorded. Resume re-applies the stored
    reply, finds the ticket already satisfied, and spends nothing: no new call."""
    store = InMemoryCallStore()
    run = "run-resume-landed"
    store.record(_recorded(run, 2, PATCH_42))
    fake = SequencedFakeOpenAI([PATCH_42])
    client = MeteredClient(store=store, prices=PRICES, client=fake, run_id=run)

    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake",
                 max_attempts=3, resume=True)
    assert r.verdict == "pass"
    assert fake.calls == 0  # recovered from the stored reply, model never re-called


def test_resume_continues_from_the_next_attempt(repo):
    """The recorded attempt was still wrong. Resume recovers it, sees validation
    fail, and continues from the next attempt, not from 1."""
    store = InMemoryCallStore()
    run = "run-resume-continue"
    store.record(_recorded(run, 1, PATCH_7))
    fake = SequencedFakeOpenAI([PATCH_42])
    client = MeteredClient(store=store, prices=PRICES, client=fake, run_id=run)

    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake",
                 max_attempts=3, resume=True)
    assert r.verdict == "pass"
    assert fake.calls == 1  # exactly one NEW attempt, not a redo of attempt 1
    assert r.results[0].record.attempt_index == 2  # resumed at 2


def test_resume_with_no_history_starts_fresh(repo):
    store = InMemoryCallStore()
    fake = SequencedFakeOpenAI([PATCH_42])
    client = MeteredClient(store=store, prices=PRICES, client=fake, run_id="never-ran")

    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake",
                 max_attempts=3, resume=True)
    assert r.verdict == "pass"
    assert r.attempts == 1


def test_resume_with_budget_already_spent_is_exhausted(repo):
    """The last allowed attempt was recorded and still fails. Resume recovers it,
    finds no budget left, and stops without a new call."""
    store = InMemoryCallStore()
    run = "run-resume-spent"
    store.record(_recorded(run, 3, PATCH_7))
    fake = SequencedFakeOpenAI([PATCH_42])
    client = MeteredClient(store=store, prices=PRICES, client=fake, run_id=run)

    r = run_loop(_ticket(VALIDATE_IS_42), repo, client, "fake",
                 max_attempts=3, resume=True)
    assert r.verdict == "exhausted"
    assert fake.calls == 0
