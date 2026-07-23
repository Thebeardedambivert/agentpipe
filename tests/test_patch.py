"""Patch tests.

Almost entirely about what it refuses. This is the module that writes to disk,
so its value is measured in the things it declines to do.

Since 23 July 2026 the model sends changes rather than whole files, so there is a
new class of refusal: an edit that cannot be located, or can be located in more
than one place. Those are the ones that make an edit *checkable*, which a
whole-file rewrite never was.
"""

from __future__ import annotations

import subprocess

import pytest

from agentpipe.patch import (
    EditBlock,
    FileEdit,
    PatchError,
    SearchReplace,
    apply_edits,
    check_edits,
    parse_blocks,
    parse_edits,
)
from agentpipe.repo import Repo

# Carries a deliberately duplicated line, so ambiguity is testable, and two
# similar returns, so line-oriented matching is testable.
THING = (
    "def one():\n"
    "    value = 0\n"
    "    return 1\n"
    "\n"
    "\n"
    "def two():\n"
    "    value = 0\n"
    "    return 111\n"
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text(THING, newline="\n")
    (tmp_path / "README.md").write_text("# readme\n", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


def block(path, search, replace):
    return (
        f"--- {path}\n"
        f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"
        f"--- end"
    )


GOOD_REPLY = block("src/thing.py", "    return 1", "    return 222")


# --- parsing the structure, no repo needed ---------------------------------

def test_parses_a_pair():
    """Note the trailing newlines: capture is line-oriented, on purpose.

    See _PAIR in patch.py. Keeping the newline is what stops a search for
    "    return 1" matching inside "    return 111".
    """
    blocks = parse_blocks(GOOD_REPLY)
    assert len(blocks) == 1
    assert blocks[0].path == "src/thing.py"
    assert blocks[0].is_new is False
    assert blocks[0].pairs[0].search == "    return 1\n"
    assert blocks[0].pairs[0].replace == "    return 222\n"


def test_matching_is_line_oriented_not_substring(repo):
    """The file holds both 'return 1' and 'return 111'. Only one may match.

    Without the trailing newline in the captured search text this would find two
    matches and be refused as ambiguous, or worse, rewrite the middle of the
    other line.
    """
    edits = parse_edits(block("src/thing.py", "    return 1", "    return 2"), repo)
    assert "    return 2\n" in edits[0].content
    assert "    return 111\n" in edits[0].content  # untouched


def test_parses_several_files_and_several_pairs():
    reply = (
        "--- a.py\n"
        "<<<<<<< SEARCH\nold one\n=======\nnew one\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\nold two\n=======\nnew two\n>>>>>>> REPLACE\n"
        "--- end\n\n"
        "--- b.py\n"
        "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"
        "--- end"
    )
    blocks = parse_blocks(reply)
    assert [b.path for b in blocks] == ["a.py", "b.py"]
    assert len(blocks[0].pairs) == 2
    assert len(blocks[1].pairs) == 1


def test_prose_instead_of_format_is_refused():
    """The most common real failure. The model explains instead of doing."""
    with pytest.raises(PatchError, match="prose instead of following the format"):
        parse_blocks("Sure! I'd be happy to help. Here's what I would change...")


def test_empty_reply_is_refused():
    with pytest.raises(PatchError, match="empty reply"):
        parse_blocks("   \n  ")


def test_same_file_twice_is_refused():
    """Not a merge to resolve. The model contradicted itself."""
    reply = block("a.py", "x", "y") + "\n\n" + block("a.py", "p", "q")
    with pytest.raises(PatchError, match="file given twice"):
        parse_blocks(reply)


def test_unterminated_block_is_refused():
    """Exactly what boltons #301 did: the whole reply, no terminator."""
    with pytest.raises(PatchError, match="no file blocks"):
        parse_blocks("--- a.py\nsome content that never ends")


def test_end_marker_inside_content_does_not_terminate_early():
    """A replacement that legitimately contains '--- end' mid-line."""
    reply = block("a.py", "x", "print('--- end of report')\nmore")
    blocks = parse_blocks(reply)
    assert "more" in blocks[0].pairs[0].replace


def test_a_whole_file_body_is_refused():
    """The old format, now rejected outright.

    This is the change. A body of plain code with no markers is exactly what the
    model used to send, and accepting it is what let a licence get rewritten.
    """
    reply = "--- src/thing.py\ndef one():\n    return 1\n--- end"
    with pytest.raises(PatchError, match="Whole-file rewrites are not accepted"):
        parse_blocks(reply)


def test_malformed_markers_get_their_own_message():
    """Tried and fumbled is a different failure from did not try."""
    reply = "--- a.py\n<<<<<<< SEARCH\nold\nnew\n>>>>>>> REPLACE\n--- end"
    with pytest.raises(PatchError, match="markers are malformed"):
        parse_blocks(reply)


def test_an_empty_search_is_refused():
    """It would match at every position: a coin flip dressed as an edit."""
    with pytest.raises(PatchError, match="would match anywhere"):
        SearchReplace(search="", replace="x")


def test_a_new_block_cannot_carry_pairs():
    with pytest.raises(PatchError, match="cannot have SEARCH blocks"):
        EditBlock(path="a.py", is_new=True, content="x",
                  pairs=(SearchReplace("a", "b"),))


def test_an_edit_block_cannot_carry_content():
    with pytest.raises(PatchError, match="cannot carry content"):
        EditBlock(path="a.py", is_new=False, content="x")


# --- resolving against the repo --------------------------------------------

def test_search_not_found_is_refused(repo):
    """The model quoted text that is not in the file."""
    reply = block("src/thing.py", "    return 999", "    return 1")
    with pytest.raises(PatchError, match="does not appear in the file"):
        parse_edits(reply, repo)


def test_ambiguous_search_is_refused(repo):
    """Two matches means it did not say which. Picking is a coin flip."""
    reply = block("src/thing.py", "    value = 0", "    value = 9")
    with pytest.raises(PatchError, match="matches 2 places"):
        parse_edits(reply, repo)


def test_ambiguity_is_resolved_by_more_context(repo):
    """The refusal is actionable: quote more lines and it becomes unique."""
    reply = block(
        "src/thing.py",
        "def one():\n    value = 0",
        "def one():\n    value = 9",
    )
    edits = parse_edits(reply, repo)
    assert "def one():\n    value = 9" in edits[0].content
    assert "def two():\n    value = 0" in edits[0].content  # the other is untouched


def test_pairs_apply_in_order_each_seeing_the_last(repo):
    """Order is the model's, and a later pair may match what an earlier inserted."""
    reply = (
        "--- src/thing.py\n"
        "<<<<<<< SEARCH\n    return 1\n=======\n    return MARKER\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n    return MARKER\n=======\n    return 42\n>>>>>>> REPLACE\n"
        "--- end"
    )
    edits = parse_edits(reply, repo)
    assert "return 42" in edits[0].content
    assert "MARKER" not in edits[0].content


def test_editing_a_missing_file_is_refused(repo):
    reply = block("src/nowhere.py", "x", "y")
    with pytest.raises(PatchError, match="no such file"):
        parse_edits(reply, repo)


def test_new_file_is_created(repo):
    """Creating a file has to be declared, and the refusal says how."""
    plain = "--- src/fresh.py\nhello = 1\n--- end"
    with pytest.raises(PatchError, match="Whole-file rewrites are not accepted"):
        parse_edits(plain, repo)

    declared = "--- src/fresh.py NEW\nhello = 1\n--- end"
    edits = parse_edits(declared, repo)
    assert edits[0].content == "hello = 1\n"


def test_new_on_an_existing_file_is_refused(repo):
    """This is the whole-file rewrite wearing a NEW badge."""
    reply = "--- src/thing.py NEW\nwiped\n--- end"
    with pytest.raises(PatchError, match="marked NEW but the file already exists"):
        parse_edits(reply, repo)


# --- the regression test for the run that caused all of this ----------------

LICENCED = '''# Copyright (c) 2013, Mahmoud Hashemi
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


def build(func):
    """Rebuild a function."""
    return None
'''


def test_a_licence_header_cannot_be_touched_by_an_edit_elsewhere(tmp_path):
    """Named for the run that made this change necessary.

    boltons #301, 23 July 2026. Asked for the complete contents of a 38,692
    character file to change a few lines, gpt-5.4-mini retyped the whole thing and
    quietly rewrote the BSD licence at the top: 'ARE DISCLAIMED' deleted from the
    warranty clause, 'LOSS OF USE, DATA, OR PROFITS;' from the damages clause. All
    445 boltons tests passed. Nothing covers a licence header, so nothing noticed.

    The guarantee is now structural rather than hoped for: text the model does not
    quote is not in the reply, so it cannot be changed. This test asserts the
    header is byte-identical, not merely present, because 'present' is what the
    corrupted version also was.
    """
    (tmp_path / "funcutils.py").write_text(LICENCED, newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    r = Repo(tmp_path)

    reply = block("funcutils.py", "    return None", "    return func()")
    written = apply_edits(r, parse_edits(reply, r))
    assert written == ("funcutils.py",)

    after = r.read("funcutils.py")
    licence_before = LICENCED.split("\n\n\ndef build")[0]
    licence_after = after.split("\n\n\ndef build")[0]
    assert licence_after == licence_before, "the licence header was modified"

    # And specifically the two clauses that were deleted in the real run.
    assert "A PARTICULAR PURPOSE ARE DISCLAIMED." in after
    assert "LOSS OF USE,\n# DATA, OR PROFITS;" in after
    # The intended change did land.
    assert "return func()" in after


# --- safety, unchanged by the format change ---------------------------------

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
    assert repo.read("src/thing.py") == THING


# --- applying ---------------------------------------------------------------

def test_applies_edits(repo):
    written = apply_edits(repo, parse_edits(GOOD_REPLY, repo))
    assert written == ("src/thing.py",)
    assert "return 111" in repo.read("src/thing.py")
    # Everything the model did not quote is untouched.
    assert "def two():" in repo.read("src/thing.py")


def test_creates_new_files_and_parent_dirs(repo):
    apply_edits(repo, (FileEdit("deep/new/file.py", "hello\n"),))
    assert repo.read("deep/new/file.py") == "hello\n"


def test_dry_run_writes_nothing(repo):
    apply_edits(repo, parse_edits(GOOD_REPLY, repo), dry_run=True)
    assert repo.read("src/thing.py") == THING


def test_apply_checks_even_if_caller_forgot(repo):
    """Never trust the caller to have called check_edits."""
    with pytest.raises(PatchError):
        apply_edits(repo, (FileEdit("../bad.py", "x\n"),))
