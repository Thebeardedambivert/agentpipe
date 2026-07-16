"""Pack tests.

The two that matter: it is deterministic, and the volatile part is last.
Everything else is decoration.
"""

from __future__ import annotations

import subprocess

import pytest

from agentpipe.pack import RULES, build
from agentpipe.repo import Repo, select
from agentpipe.ticket import Ticket


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "telemetry.py").write_text("# the meter\n" * 20)
    (tmp_path / "README.md").write_text("# readme\n" * 5)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


@pytest.fixture
def ticket():
    return Ticket.parse("""# TASK-1

## Goal
The telemetry module should record cached tokens so the cache discount is visible.

## Validation
```
pytest -q
```

## Acceptance
- [ ] cached_input_tokens is populated
- [ ] a test covers it

## Constraints
- Do not change the schema
""")


def built(ticket, repo, feedback=None):
    return build(ticket, repo, select(ticket, repo), feedback=feedback)


# --- the property everything else depends on ------------------------------

def test_same_inputs_produce_the_same_hash(ticket, repo):
    assert built(ticket, repo).hash == built(ticket, repo).hash


def test_different_feedback_produces_a_different_hash(ticket, repo):
    """Attempt 2 is genuinely different work, and should be paid for."""
    a = built(ticket, repo)
    b = built(ticket, repo, feedback="AssertionError on line 40")
    assert a.hash != b.hash


def test_a_changed_repo_produces_a_different_hash(ticket, repo, tmp_path):
    before = built(ticket, repo).hash
    (tmp_path / "src" / "telemetry.py").write_text("# changed\n")
    assert built(ticket, repo).hash != before


# --- ordering, which is the money ----------------------------------------

def test_rules_come_first_and_never_vary(ticket, repo):
    p = built(ticket, repo)
    assert p.messages[0]["role"] == "system"
    assert p.messages[0]["content"] == RULES


def test_system_prompt_is_identical_across_different_tickets(repo, ticket):
    """The cache prefix. If this ever varies, every call pays full price."""
    other = Ticket.parse("""# TASK-2

## Goal
Something completely different about the readme file and its contents.

## Validation
```
pytest -q
```

## Acceptance
- [ ] done
""")
    assert built(ticket, repo).messages[0] == built(other, repo).messages[0]


def test_tree_precedes_file_contents(ticket, repo):
    body = built(ticket, repo).messages[1]["content"]
    assert body.index("Files in this repository") < body.index("--- src/telemetry.py")


def test_feedback_goes_last(ticket, repo):
    """The most volatile thing in the pack sits at the very bottom."""
    body = built(ticket, repo, feedback="boom").messages[1]["content"]
    assert body.index("The last attempt failed") > body.index("## Goal")
    assert body.rstrip().endswith("boom")


def test_adding_feedback_does_not_disturb_the_prefix(ticket, repo):
    """The whole cache argument in one assertion."""
    plain = built(ticket, repo).messages[1]["content"]
    with_fb = built(ticket, repo, feedback="boom").messages[1]["content"]
    assert with_fb.startswith(plain)


# --- contents -------------------------------------------------------------

def test_validation_commands_reach_the_model(ticket, repo):
    assert "pytest -q" in built(ticket, repo).messages[1]["content"]


def test_constraints_reach_the_model(ticket, repo):
    assert "Do not change the schema" in built(ticket, repo).messages[1]["content"]


def test_tokens_are_estimated(ticket, repo):
    assert built(ticket, repo).tokens > 0


def test_no_previous_attempts_parameter():
    """The 70k fix, enforced by the signature.

    build() cannot accept prior attempts because there is nowhere to put them.
    Context is rebuilt from the repo, never accumulated. If someone ever adds a
    history parameter here, this test should be the thing that argues with them.
    """
    import inspect
    params = set(inspect.signature(build).parameters)
    assert params == {"ticket", "repo", "selected", "feedback"}
