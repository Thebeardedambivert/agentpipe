"""Repo and selection tests.

These build a real throwaway git repo rather than mocking git. Mocking
subprocess would prove our mock works, which is not a thing anyone needs to
know.
"""

from __future__ import annotations

import subprocess

import pytest

from agentpipe.repo import Candidate, Repo, RepoError, estimate_tokens, select
from agentpipe.ticket import Ticket


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "telemetry.py").write_text("# the meter\n" * 50)
    (tmp_path / "src" / "ticket.py").write_text("# the contract\n" * 30)
    (tmp_path / "README.md").write_text("# readme\n" * 10)
    (tmp_path / "prices.example.json").write_text("{}\n")
    (tmp_path / "secret.png").write_bytes(b"\x89PNG binary")
    (tmp_path / "ignored.txt").write_text("should not appear")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


def ticket_with(goal: str, files: str = "") -> Ticket:
    return Ticket.parse(f"""# TASK-1

## Goal
{goal}

## Validation
```
pytest -q
```

## Acceptance
- [ ] it works

## Files
{files}
""")


# --- reading --------------------------------------------------------------

def test_lists_tracked_text_files(repo):
    files = repo.files()
    assert "README.md" in files
    assert "src/telemetry.py" in files


def test_gitignored_files_are_invisible(repo):
    """We never wrote ignore logic. Git did it for us."""
    assert "ignored.txt" not in repo.files()


def test_binaries_are_skipped(repo):
    assert "secret.png" not in repo.files()


def test_tree_is_cheap(repo):
    """The whole point: names cost almost nothing."""
    assert estimate_tokens(repo.tree()) < 50


def test_not_a_git_repo_is_an_error(tmp_path):
    with pytest.raises(RepoError, match="not a git repository"):
        Repo(tmp_path)


def test_path_traversal_is_refused(repo):
    """A ticket is untrusted input. It can ask for ../../.env and mean it."""
    with pytest.raises(RepoError, match="escapes the repository"):
        repo.read("../../../etc/passwd")


def test_missing_file_is_an_error(repo):
    with pytest.raises(RepoError, match="no such file"):
        repo.read("nope.py")


# --- selection ------------------------------------------------------------

def test_ticket_hints_win(repo):
    t = ticket_with(
        "Something entirely unrelated to any filename in this repository at all",
        "- prices.example.json",
    )
    picked = select(t, repo)
    assert picked[0].path == "prices.example.json"
    assert picked[0].reason == "named in ticket"


def test_goal_words_match_paths(repo):
    t = ticket_with("The telemetry module should record cached tokens correctly")
    picked = select(t, repo)
    assert "src/telemetry.py" in [c.path for c in picked]


def test_hints_outrank_word_matches(repo):
    t = ticket_with(
        "The telemetry module should record cached tokens correctly",
        "- README.md",
    )
    picked = select(t, repo)
    assert picked[0].path == "README.md"


def test_hint_for_a_file_that_does_not_exist_is_ignored(repo):
    """A typo in a ticket should not crash the pipeline."""
    t = ticket_with("The telemetry module needs a fix applied to it", "- ghost.py")
    picked = select(t, repo)
    assert "ghost.py" not in [c.path for c in picked]


def test_respects_max_files(repo):
    t = ticket_with("telemetry ticket readme prices example json src module")
    assert len(select(t, repo, max_files=2)) == 2


def test_selection_is_deterministic(repo):
    """Layer 1's entire promise. Same inputs, same output, every time."""
    t = ticket_with("The telemetry module should record cached tokens correctly")
    assert select(t, repo) == select(t, repo)


def test_no_match_returns_nothing_rather_than_guessing(repo):
    t = ticket_with("Zzzz qqqq wwww vvvv unrelated nonsense words here")
    assert select(t, repo) == ()
