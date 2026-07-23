"""Turning a model's reply into files on disk.

This is the first module that writes anything, and the first that treats model
output as what it actually is: untrusted text that happens to usually be right.

Two jobs, and the second one is the dangerous one:

1. Parse the reply into file edits, refusing anything ambiguous
2. Apply those edits, refusing anything that escapes the repository

The parser is strict on purpose. A forgiving parser is one that will eventually
understand a malformed reply *incorrectly* and write nonsense into your working
tree with complete confidence. Refusing costs one retry. Guessing costs a repo.

Why the model sends changes and not whole files
-----------------------------------------------

It used to ask for "the complete new contents of each file you change". On
23 July 2026 that was pointed at `mahmoud/boltons` for the first real
third-party run, on its real open issue #301. To change a few lines the model
retyped a 38,692 character file, and while retyping it edited the BSD licence:
`ARE DISCLAIMED` vanished from the warranty clause, `LOSS OF USE, DATA, OR
PROFITS;` from the damages clause. Nobody asked it to touch the licence. No test
covers a licence header, so nothing anywhere would have noticed. It cost $0.047,
about 85% of it output, because retyping ties the bill to file size rather than
change size.

The whole-file rule did not just cost more. It **granted the model licence to
alter anything in a file while it was retyping it**. That is the actual defect,
and it is a safety defect.

So the model now quotes the exact text it wants to change and what to change it
to. Text it does not quote cannot be altered, because it never appears in the
reply at all. The guarantee is structural rather than hoped for, which is the
same move as `CallRecord.__post_init__`: prefer making the bad state
unrepresentable to guarding against it at every site.

The format
----------

Editing a file that exists:

    --- path/to/file.py
    <<<<<<< SEARCH
    the exact text to find
    =======
    what to put there instead
    >>>>>>> REPLACE
    --- end

Creating a file that does not exist, the only case where full content is legal
because there is nothing there to corrupt:

    --- path/to/new_file.py NEW
    the full contents
    --- end

Several pairs per file, several files per reply. Pairs apply in order, each to
the result of the last.

Exact matching earns something the old format could never offer: the edit is
*checkable*. A SEARCH that matches nothing, or matches twice, is refused rather
than guessed at. A whole-file rewrite could not be checked against anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agentpipe.repo import Repo, RepoError

# One file's block. The optional NEW marker is the only way to send whole
# content, and check_new refuses it when the path already exists.
#
# Non-greedy body, anchored to line starts, so a file containing the literal
# text "--- end" mid-line cannot terminate its own block early.
_BLOCK = re.compile(
    r"^--- (?P<path>\S+)(?P<new>[ \t]+NEW)?[ \t]*\n(?P<body>.*?)^--- end\s*$",
    re.MULTILINE | re.DOTALL,
)

# One search/replace pair inside a block. The markers are the conventional ones,
# so a model that has seen this format anywhere has seen these exact characters.
#
# Both groups capture up to the start of the next marker line, which means the
# newline that ends the last quoted line is part of the captured text. That is
# deliberate and it is a safety property, not an accident of the regex: matching
# is therefore line-oriented. Searching for "    return 1\n" cannot match inside
# "    return 111\n", whereas a newline-stripped "    return 1" would, and would
# then rewrite the middle of a line the model never intended to touch.
_PAIR = re.compile(
    r"^<{7} SEARCH[ \t]*\n(?P<search>.*?)^={7}[ \t]*\n(?P<replace>.*?)^>{7} REPLACE[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# Anything that looks like a pair marker but is not part of a well-formed pair.
# Used to tell "the model wrote prose" apart from "the model tried and fumbled",
# because those deserve different messages back.
_ANY_MARKER = re.compile(r"^(?:<{7}|={7}|>{7})", re.MULTILINE)


class PatchError(Exception):
    """The reply could not be turned into edits we are willing to apply."""


@dataclass(frozen=True)
class SearchReplace:
    """One exact-text substitution. Validated at construction."""

    search: str
    replace: str

    def __post_init__(self) -> None:
        if not self.search:
            # An empty search matches at every position. Applying it would be a
            # coin flip dressed as an edit.
            raise PatchError("a SEARCH block is empty; it would match anywhere")


@dataclass(frozen=True)
class EditBlock:
    """One file's worth of instructions, before we have looked at the file.

    Two legal shapes and no others, enforced here so a third cannot be built:
    a NEW block carries content and no pairs, an edit block carries pairs and no
    content.
    """

    path: str
    is_new: bool
    pairs: tuple[SearchReplace, ...] = ()
    content: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise PatchError("edit block has no path")
        if self.is_new:
            if self.pairs:
                raise PatchError(f"{self.path}: a NEW file cannot have SEARCH blocks")
            if self.content is None:
                raise PatchError(f"{self.path}: NEW file has no content")
        else:
            if self.content is not None:
                raise PatchError(f"{self.path}: an edit block cannot carry content")
            if not self.pairs:
                raise PatchError(f"{self.path}: edit block has no SEARCH/REPLACE pairs")


@dataclass(frozen=True)
class FileEdit:
    """A file and its complete new contents, ready to write.

    Unchanged, deliberately. This is still what gets applied, so check_edits and
    apply_edits keep every guarantee they already had. Only the way `content`
    gets derived has changed.
    """

    path: str
    content: str


def parse_blocks(reply: str) -> tuple[EditBlock, ...]:
    """Read the reply's structure. Pure: no repo, no filesystem, no I/O.

    Split out from resolution so the format itself can be tested without a
    working tree, and so a malformed reply is rejected before anything is read.
    """
    if not reply.strip():
        raise PatchError("empty reply")

    matches = list(_BLOCK.finditer(reply))
    if not matches:
        preview = reply.strip()[:200].replace("\n", " ")
        raise PatchError(
            f"no file blocks found. The model replied with prose instead of "
            f"following the format. First 200 chars: {preview!r}"
        )

    blocks: list[EditBlock] = []
    seen: set[str] = set()
    for m in matches:
        path = m.group("path").strip()
        if path in seen:
            # Two versions of one file is not a merge problem we should be
            # solving. It means the model contradicted itself.
            raise PatchError(f"file given twice, cannot choose: {path}")
        seen.add(path)

        body = m.group("body")
        if m.group("new"):
            blocks.append(EditBlock(path=path, is_new=True, content=body))
            continue

        pairs = tuple(
            SearchReplace(search=p.group("search"), replace=p.group("replace"))
            for p in _PAIR.finditer(body)
        )
        if not pairs:
            # Tell the two failures apart. A body full of code with no markers is
            # the old whole-file habit; stray markers mean it tried and fumbled.
            if _ANY_MARKER.search(body):
                raise PatchError(
                    f"{path}: SEARCH/REPLACE markers are malformed. Each pair "
                    f"needs '<<<<<<< SEARCH', then '=======', then "
                    f"'>>>>>>> REPLACE', each alone on its line."
                )
            raise PatchError(
                f"{path}: no SEARCH/REPLACE pairs. Whole-file rewrites are not "
                f"accepted for a file that already exists: send only the text "
                f"you are changing. Use '--- {path} NEW' only to create a file "
                f"that does not exist yet."
            )
        blocks.append(EditBlock(path=path, is_new=False, pairs=pairs))

    return tuple(blocks)


def _at_end_of_file(content: str, pair: SearchReplace) -> tuple[str, str]:
    """Handle a quoted last line in a file that does not end with a newline.

    The captured search text always ends with a newline, because the block format
    puts a marker on the next line (see _PAIR). A file whose final line has no
    newline therefore cannot be matched literally, and the model is not at fault:
    that newline is an artefact of how the reply is delimited, not something it
    claimed was there.

    This is narrow on purpose and is not a fallback to fuzzy matching. It applies
    only when the trailing newline is the sole obstacle AND the text sits at the
    very end of the file, which is the one place a missing newline is meaningful.
    Anywhere else, a mismatch is still a mismatch and still refused.
    """
    if pair.search in content or not pair.search.endswith("\n"):
        return pair.search, pair.replace

    trimmed = pair.search[:-1]
    if trimmed and content.endswith(trimmed):
        return trimmed, pair.replace.removesuffix("\n")
    return pair.search, pair.replace


def resolve_edits(blocks: tuple[EditBlock, ...], repo: Repo) -> tuple[FileEdit, ...]:
    """Turn blocks into complete file contents, against the repo as it is now.

    Pairs apply in order, each to the result of the one before, so a later pair
    can legitimately match text an earlier one inserted. Order is the model's,
    and it is preserved rather than reordered.

    Every refusal here is a refusal to guess. A SEARCH matching nothing means the
    model quoted text that is not in the file. A SEARCH matching twice means it
    did not say which one it meant, and picking is a coin flip that lands in
    somebody's working tree.
    """
    edits: list[FileEdit] = []

    for block in blocks:
        exists = (repo.root / block.path).is_file()

        if block.is_new:
            if exists:
                raise PatchError(
                    f"{block.path}: marked NEW but the file already exists. "
                    f"Refusing to replace it wholesale; send SEARCH/REPLACE pairs."
                )
            edits.append(FileEdit(path=block.path, content=block.content or ""))
            continue

        if not exists:
            raise PatchError(
                f"{block.path}: no such file. To create it, mark the block "
                f"'--- {block.path} NEW' and send its full contents."
            )

        content = repo.read(block.path)
        for i, pair in enumerate(block.pairs):
            search, replace = _at_end_of_file(content, pair)
            found = content.count(search)
            if found == 0:
                preview = pair.search.strip().splitlines()[:2]
                raise PatchError(
                    f"{block.path}: SEARCH block {i} does not appear in the file. "
                    f"It must match the current text exactly, whitespace included. "
                    f"Looked for: {' / '.join(preview)!r}"
                )
            if found > 1:
                raise PatchError(
                    f"{block.path}: SEARCH block {i} matches {found} places in the "
                    f"file, so which one to change is ambiguous. Include enough "
                    f"surrounding lines to make it unique."
                )
            content = content.replace(search, replace, 1)

        edits.append(FileEdit(path=block.path, content=content))

    return tuple(edits)


def parse_edits(reply: str, repo: Repo) -> tuple[FileEdit, ...]:
    """Pull file edits out of a model reply.

    Deliberately unforgiving. We do not strip markdown fences, guess at near-miss
    delimiters, accept an unterminated block, or fall back to treating a body as
    whole-file content. If the model did not follow the format, the correct
    response is to say so and try again, not to reconstruct what it probably
    meant.

    Takes the repo because an edit is now relative to what is on disk. That is
    the point: the reply describes a change, and a change has no meaning without
    the thing it changes.
    """
    return resolve_edits(parse_blocks(reply), repo)


def check_edits(
    repo: Repo,
    edits: tuple[FileEdit, ...],
    allowed: tuple[str, ...] | None = None,
) -> None:
    """Refuse anything we should not write, before writing any of it.

    Checks everything up front rather than as it goes. A half-applied patch is
    worse than a rejected one: the working tree ends up in a state that neither
    the model nor the repo ever intended, and nobody knows which files are new.
    """
    problems: list[str] = []

    for e in edits:
        full = (repo.root / e.path).resolve()

        # A model is untrusted input. "../../../.ssh/authorized_keys" is a
        # perfectly plausible completion under the right circumstances.
        if not full.is_relative_to(repo.root):
            problems.append(f"escapes the repository: {e.path}")
            continue

        if ".git" in Path(e.path).parts:
            problems.append(f"touches git internals: {e.path}")
            continue

        # The ticket named the files it expected to change. A patch reaching
        # outside that set is not necessarily wrong, but it is not what was
        # agreed, and a human should look.
        if allowed is not None and e.path not in allowed:
            problems.append(
                f"not in the agreed file set: {e.path} "
                f"(ticket allowed: {', '.join(allowed) or 'nothing'})"
            )

        if not e.content.strip():
            problems.append(f"empty content for {e.path}")

    if problems:
        raise PatchError(
            "refusing to apply:\n" + "\n".join(f"  - {p}" for p in problems)
        )


def apply_edits(
    repo: Repo,
    edits: tuple[FileEdit, ...],
    allowed: tuple[str, ...] | None = None,
    dry_run: bool = False,
) -> tuple[str, ...]:
    """Write the edits. Returns the paths written.

    Always call check_edits first, and it does. Never trust a caller to have
    remembered.
    """
    check_edits(repo, edits, allowed)

    if dry_run:
        return tuple(e.path for e in edits)

    written: list[str] = []
    for e in edits:
        full = repo.root / e.path
        full.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" so a Windows machine does not rewrite every line ending
        # and turn a two-line change into a whole-file diff.
        full.write_text(e.content, encoding="utf-8", newline="\n")
        written.append(e.path)

    return tuple(written)


def summarise(edits: tuple[FileEdit, ...], repo: Repo) -> str:
    """What changed, roughly, for a human reading a log."""
    lines = []
    for e in edits:
        try:
            before = len(repo.read(e.path).splitlines())
            verb = "modify"
        except RepoError:
            before = 0
            verb = "create"
        after = len(e.content.splitlines())
        lines.append(f"  {verb:<7} {e.path:<32} {before:>4} -> {after:>4} lines")
    return "\n".join(lines)
