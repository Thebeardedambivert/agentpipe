"""Turning a model's reply into files on disk.

This is the first module that writes anything, and the first that treats model
output as what it actually is: untrusted text that happens to usually be right.

Two jobs, and the second one is the dangerous one:

1. Parse the reply into file edits, refusing anything ambiguous
2. Apply those edits, refusing anything that escapes the repository

The parser is strict on purpose. A forgiving parser is one that will eventually
understand a malformed reply *incorrectly* and write nonsense into your working
tree with complete confidence. Refusing costs one retry. Guessing costs a repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentpipe.repo import Repo, RepoError

# Matches the format RULES asks for:
#
#   --- path/to/file.py
#   <contents>
#   --- end
#
# Non-greedy body, anchored to line starts, so a file containing the literal
# text "--- end" mid-line cannot terminate its own block early.
_BLOCK = re.compile(
    r"^--- (?P<path>\S+)\n(?P<body>.*?)^--- end\s*$",
    re.MULTILINE | re.DOTALL,
)


class PatchError(Exception):
    """The reply could not be turned into edits we are willing to apply."""


@dataclass(frozen=True)
class FileEdit:
    path: str
    content: str


def parse_edits(reply: str) -> tuple[FileEdit, ...]:
    """Pull file edits out of a model reply.

    Deliberately unforgiving. We do not strip markdown fences, guess at
    near-miss delimiters, or accept a single unterminated block. If the model
    did not follow the format, the correct response is to say so and try again,
    not to reconstruct what it probably meant.
    """
    if not reply.strip():
        raise PatchError("empty reply")

    blocks = list(_BLOCK.finditer(reply))
    if not blocks:
        preview = reply.strip()[:200].replace("\n", " ")
        raise PatchError(
            f"no file blocks found. The model replied with prose instead of "
            f"following the format. First 200 chars: {preview!r}"
        )

    edits: list[FileEdit] = []
    seen: set[str] = set()
    for m in blocks:
        path = m.group("path").strip()
        if path in seen:
            # Two versions of one file is not a merge problem we should be
            # solving. It means the model contradicted itself.
            raise PatchError(f"file given twice, cannot choose: {path}")
        seen.add(path)
        edits.append(FileEdit(path=path, content=m.group("body")))

    return tuple(edits)


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
