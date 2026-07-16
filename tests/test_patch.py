"""Patch tests.

Almost entirely about what it refuses. This is the module that writes to disk,
so its value is measured in the things it declines to do.
"""

from __future__ import annotations

import subprocess

import pytest

from agentpipe.patch import (
    FileEdit,
    PatchError,
    apply_edits,
    check_edits,
    parse_edits,
)
from agentpipe.repo import Repo


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("old\n")
    (tmp_path / "README.md").write_text("# readme\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


GOOD_REPLY = """--- src/thing.py
new content
line two
--- end

--- README.md
# readme
updated
--- end"""


# --- parsing --------------------------------------------------------------

def test_parses_multiple_files():
    edits = parse_edits(GOOD_REPLY)
    assert [e.path for e in edits] == ["src/thing.py", "README.md"]
    assert edits[0].content == "new content\nline two\n"


def test_prose_instead_of_format_is_refused():
    """The most common real failure. The model explains instead of doing."""
    with pytest.raises(PatchError, match="prose instead of following the format"):
        parse_edits("Sure! I'd be happy to help. Here's what I would change...")


def test_empty_reply_is_refused():
    with pytest.raises(PatchError, match="empty reply"):
        parse_edits("   \n  ")


def test_same_file_twice_is_refused():
    """Not a merge to resolve. The model contradicted itself."""
    reply = "--- a.py\nfirst\n--- end\n\n--- a.py\nsecond\n--- end"
    with pytest.raises(PatchError, match="file given twice"):
        parse_edits(reply)


def test_unterminated_block_is_refused():
    """Truncated response, hit a token limit. Do not apply half a file."""
    with pytest.raises(PatchError, match="no file blocks"):
        parse_edits("--- a.py\nsome content that never ends")


def test_end_marker_inside_content_does_not_terminate_early():
    """A file that legitimately contains '--- end' mid-line."""
    reply = "--- a.py\nprint('--- end of report')\nmore\n--- end"
    edits = parse_edits(reply)
    assert "more" in edits[0].content


# --- safety ---------------------------------------------------------------

def test_path_escaping_the_repo_is_refused(repo):
    edits = (FileEdit("../../../.ssh/authorized_keys", "pwned\n"),)
    with pytest.raises(PatchError, match="escapes the repository"):
        check_edits(repo, edits)


def test_writing_to_git_internals_is_refused(repo):
    edits = (FileEdit(".git/config", "evil\n"),)
    with pytest.raises(PatchError, match="git internals"):
        check_edits(repo, edits)


def test_files_outside_the_agreed_set_are_refused(repo):
    edits = (FileEdit("src/thing.py", "x\n"), FileEdit("sneaky.py", "y\n"))
    with pytest.raises(PatchError, match="not in the agreed file set"):
        check_edits(repo, edits, allowed=("src/thing.py",))


def test_empty_content_is_refused(repo):
    with pytest.raises(PatchError, match="empty content"):
        check_edits(repo, (FileEdit("src/thing.py", "   \n"),))


def test_all_problems_reported_before_anything_is_written(repo):
    edits = (
        FileEdit("../escape.py", "x\n"),
        FileEdit(".git/config", "y\n"),
    )
    with pytest.raises(PatchError) as exc:
        check_edits(repo, edits)
    assert "escapes" in str(exc.value)
    assert "git internals" in str(exc.value)


def test_nothing_is_written_when_any_edit_is_bad(repo):
    """A half-applied patch is worse than a rejected one."""
    edits = (FileEdit("src/thing.py", "good\n"), FileEdit("../bad.py", "x\n"))
    with pytest.raises(PatchError):
        apply_edits(repo, edits)
    assert repo.read("src/thing.py") == "old\n"


# --- applying -------------------------------------------------------------

def test_applies_edits(repo):
    written = apply_edits(repo, parse_edits(GOOD_REPLY))
    assert set(written) == {"src/thing.py", "README.md"}
    assert repo.read("src/thing.py") == "new content\nline two\n"


def test_creates_new_files_and_parent_dirs(repo):
    apply_edits(repo, (FileEdit("deep/new/file.py", "hello\n"),))
    assert repo.read("deep/new/file.py") == "hello\n"


def test_dry_run_writes_nothing(repo):
    apply_edits(repo, parse_edits(GOOD_REPLY), dry_run=True)
    assert repo.read("src/thing.py") == "old\n"


def test_apply_checks_even_if_caller_forgot(repo):
    """Never trust the caller to have called check_edits."""
    with pytest.raises(PatchError):
        apply_edits(repo, (FileEdit("../bad.py", "x\n"),))
