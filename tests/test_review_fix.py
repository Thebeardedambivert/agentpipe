"""Fixer-loop tests.

A fake model that answers review calls and fix calls from separate scripted
queues, keyed on the system prompt, so review and fix can be driven independently
without a real API. Validation is a real portable `python -c` command whose
pass/fail depends on what the fake fixer wrote, so the fix -> revalidate -> revert
cycle runs for real without a model.

The stage's whole promise is proved here: a fix that breaks validation is undone,
byte for byte, so the loop can never leave working code worse.

Self-contained by design (own fake, no import from another test module): a bare
`pytest`, as CI runs it, has no repo root on sys.path.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agentpipe.config import ModelMap
from agentpipe.fixer import _restore, _snapshot, run_review_fix
from agentpipe.repo import Repo
from agentpipe.telemetry import InMemoryCallStore, MeteredClient, PriceMap
from agentpipe.ticket import Ticket

PRICES = PriceMap({
    "base-m": {"input": 1.0, "cached_input": 0.1, "output": 10.0},
    "rev-m": {"input": 1.0, "cached_input": 0.1, "output": 10.0},
    "fix-m": {"input": 1.0, "cached_input": 0.1, "output": 10.0},
})

# Validation commands, portable so Windows dev and Linux CI behave the same.
VALIDATE_NONEMPTY = (
    'python -c "import os,sys; sys.exit(0 if os.path.exists(\'answer.txt\') '
    "and open('answer.txt').read().strip() else 1)\""
)
VALIDATE_HAS_42 = (
    'python -c "import sys; sys.exit(0 if \'42\' in open(\'answer.txt\').read() else 1)"'
)

# Fixer replies, in the --- file block format FIX_RULES asks for.
def patch(content: str) -> str:
    return f"--- answer.txt\n{content}\n--- end"

PROSE = "I had a look and it seems fine to me."

# Reviewer findings.
def review(*findings: dict) -> str:
    return "--- findings\n" + json.dumps(list(findings)) + "\n--- end"

def high(issue: str = "should say 42") -> dict:
    return {"severity": "high", "file": "answer.txt", "line": 1, "issue": issue}

LOW = {"severity": "low", "file": "answer.txt", "line": 1, "issue": "minor style nit"}


class FakeModel:
    """Answers review calls from `reviews` and fix calls from `fixes`, keyed on the
    system prompt. Repeats the last reply if calls outrun the queue. Records which
    model each call used, so routing can be asserted."""

    def __init__(self, reviews: list[str], fixes: list[str]) -> None:
        self.reviews = list(reviews)
        self.fixes = list(fixes)
        self.review_calls = 0
        self.fix_calls = 0
        self.review_models: list[str] = []
        self.fix_models: list[str] = []

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        if "fixing one specific problem" in system:
            reply = self.fixes[min(self.fix_calls, len(self.fixes) - 1)]
            self.fix_calls += 1
            self.fix_models.append(kwargs["model"])
        else:
            reply = self.reviews[min(self.review_calls, len(self.reviews) - 1)]
            self.review_calls += 1
            self.review_models.append(kwargs["model"])
        return SimpleNamespace(
            model=kwargs["model"],
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=reply),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=1200,
                completion_tokens=80,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def make(reviews, fixes, base="base-m", overrides=None):
    fake = FakeModel(reviews, fixes)
    client = MeteredClient(
        store=InMemoryCallStore(), prices=PRICES,
        client=fake, run_id="fix-run",  # type: ignore[arg-type]
    )
    return client, fake, ModelMap(base, overrides)


def _ticket(validation: str) -> Ticket:
    return Ticket.parse(
        f"""# TASK-FIX

## Goal
answer.txt should hold the value the ticket needs, and stay valid after edits.

## Validation
```
{validation}
```

## Acceptance
- [ ] answer.txt is correct

## Files
- answer.txt
"""
    )


@pytest.fixture
def repo(tmp_path):
    def _make(content: str) -> Repo:
        (tmp_path / "answer.txt").write_text(content, encoding="utf-8", newline="\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        return Repo(tmp_path)
    return _make


FILES = ("answer.txt",)


# --- the heart of the stage: keep good fixes, revert bad ones ---------------

def test_a_good_fix_is_kept(repo):
    """Validation still passes after the fix, so it stays and the finding is fixed."""
    r = repo("7")  # non-empty, so validation passes before review
    client, fake, models = make([review(high()), review()], [patch("42")])
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES,
                            max_rounds=3)
    assert result.stopped == "clean"
    assert [rd.outcome for rd in result.rounds] == ["fixed"]
    assert r.read("answer.txt").strip() == "42"


def test_a_breaking_fix_is_reverted_byte_for_byte(repo):
    """The fix drops '42' and validation now fails, so it is undone and the file is
    exactly what it was. This is the guarantee the whole stage exists for."""
    r = repo("42 start")  # contains 42, so validation passes before review
    client, fake, models = make([review(high())], [patch("no number here")])
    result = run_review_fix(_ticket(VALIDATE_HAS_42), r, client, models, FILES,
                            max_rounds=3)
    assert [rd.outcome for rd in result.rounds] == ["reverted"]
    assert r.read("answer.txt") == "42 start"  # unchanged, byte for byte
    assert result.stopped == "settled"  # the reverted finding is not retried


def test_snapshot_and_restore_delete_a_created_file(repo):
    """The in-memory revert restores edited files and deletes ones the fix created."""
    r = repo("original")
    files = ("answer.txt", "new.txt")
    snap = _snapshot(r, files)  # new.txt does not exist yet, so it is not captured
    (r.root / "answer.txt").write_text("changed", encoding="utf-8", newline="\n")
    (r.root / "new.txt").write_text("created", encoding="utf-8", newline="\n")

    _restore(r, files, snap)

    assert r.read("answer.txt") == "original"
    assert not (r.root / "new.txt").exists()


# --- stopping for the right reasons ----------------------------------------

def test_clean_review_does_nothing(repo):
    r = repo("42")
    client, fake, models = make([review()], [patch("x")])
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES)
    assert result.stopped == "clean"
    assert result.rounds == ()
    assert fake.fix_calls == 0


def test_only_low_findings_are_left_alone(repo):
    """min_severity defaults to medium, so a lone low nitpick is not acted on."""
    r = repo("42")
    client, fake, models = make([review(LOW)], [patch("x")])
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES)
    assert result.stopped == "settled"
    assert result.rounds == ()
    assert fake.fix_calls == 0


def test_a_low_finding_is_fixed_when_the_threshold_is_low(repo):
    r = repo("7")
    client, fake, models = make([review(LOW), review()], [patch("42")])
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES,
                            min_severity="low")
    assert [rd.outcome for rd in result.rounds] == ["fixed"]


def test_round_cap_is_respected(repo):
    """Distinct findings every round, all fixed; the run stops at max_rounds."""
    r = repo("start")
    reviews = [review(high("a")), review(high("b")), review(high("c"))]
    fixes = [patch("aa"), patch("bb"), patch("cc")]
    client, fake, models = make(reviews, fixes)
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES,
                            max_rounds=2)
    assert result.stopped == "max_rounds"
    assert len(result.rounds) == 2
    assert all(rd.outcome == "fixed" for rd in result.rounds)


# --- refusing what it cannot use -------------------------------------------

def test_malformed_review_stops_without_fixing(repo):
    r = repo("42")
    client, fake, models = make([PROSE], [patch("x")])
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES)
    assert result.stopped == "review_unparseable"
    assert result.rounds == ()
    assert fake.fix_calls == 0


def test_an_unparseable_fix_is_marked_unfixable_and_skipped(repo):
    r = repo("42 here")
    client, fake, models = make([review(high())], [PROSE])
    result = run_review_fix(_ticket(VALIDATE_HAS_42), r, client, models, FILES,
                            max_rounds=3)
    assert [rd.outcome for rd in result.rounds] == ["unfixable"]
    assert r.read("answer.txt") == "42 here"  # nothing applied
    assert result.stopped == "settled"  # not retried forever


# --- routing and attribution ------------------------------------------------

def test_the_fixer_runs_on_the_routed_model_and_is_recorded(repo):
    """The fixer uses the fixer model, the reviewer the reviewer model, and the fix
    call is recorded as role=fixer / review_fix so Stage 3 can attribute it."""
    r = repo("7")
    client, fake, models = make(
        [review(high()), review()], [patch("42")],
        base="base-m", overrides={"reviewer": "rev-m", "fixer": "fix-m"},
    )
    result = run_review_fix(_ticket(VALIDATE_NONEMPTY), r, client, models, FILES)

    assert fake.fix_models == ["fix-m"]
    assert fake.review_models[0] == "rev-m"
    rec = result.rounds[0].fix_record
    assert rec.role == "fixer"
    assert rec.attempt_kind == "review_fix"
    assert rec.model == "fix-m"
