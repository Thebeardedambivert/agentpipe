"""The repository, as the pack sees it.

Two jobs: know what files exist, and decide which few are worth paying for.

The second one is where this whole project's problem lives. Every file whose
contents go into the pack is tokens, and tokens are the invoice. "Include the
repo" is how you get to 70,000 input tokens for a two-line change.

The saving grace is that names and contents have wildly different prices:

    every path in this repo      ~200 tokens
    every file's contents        ~15,000 tokens

So the pack gets the whole tree (cheap, and the model can see what exists) plus
the contents of a handful (expensive, and it actually needs those). A catalogue
and three books, not the library.
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from agentpipe.ticket import Ticket

# Rough, and honest about it. Real tokenisers cost a dependency and a model
# round trip to be exact. For deciding "is this pack 2k or 50k" the rule of
# thumb is fine, and being approximately right in advance beats being exactly
# right afterwards.
CHARS_PER_TOKEN = 4

BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".whl",
    ".pyc", ".woff", ".woff2", ".ttf", ".mp4", ".webp",
})


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    """A file, and why we think it matters."""
    path: str
    score: float
    reason: str


class Repo:
    """Read-only view of a git repository.

    Read-only on purpose. Layer 1 decides what the agent sees. It never decides
    what the agent does. Keeping those apart is what lets us test the expensive
    half without ever risking the working tree.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists():
            raise RepoError(f"{self.root} is not a git repository")

    def files(self) -> tuple[str, ...]:
        """Every tracked text file, as posix paths relative to the root.

        Uses `git ls-files` rather than walking the tree. Git already knows what
        is tracked, already applies .gitignore, and is never out of date with
        it. Reimplementing that would mean maintaining a second, worse copy of
        rules that already exist, and being wrong about .venv forever.
        """
        try:
            out = subprocess.run(
                ["git", "ls-files"],
                cwd=self.root, capture_output=True, text=True, check=True,
            ).stdout
        except FileNotFoundError as exc:
            raise RepoError("git is not on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise RepoError(f"git ls-files failed: {exc.stderr}") from exc

        return tuple(
            sorted(
                line for line in out.splitlines()
                if line and Path(line).suffix.lower() not in BINARY_SUFFIXES
            )
        )

    def tree(self) -> str:
        """The cheap half of the pack. Every path, one per line."""
        return "\n".join(self.files())

    def read(self, path: str) -> str:
        """The expensive half. One file's contents."""
        full = (self.root / path).resolve()
        # A ticket is untrusted input. Its Files section could say
        # "../../../.env" and mean it.
        if not full.is_relative_to(self.root):
            raise RepoError(f"path escapes the repository: {path}")
        if not full.exists():
            raise RepoError(f"no such file: {path}")
        return full.read_text(encoding="utf-8", errors="replace")


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


def _idf(paths: tuple[str, ...]) -> dict[str, float]:
    """How much a word is worth as evidence.

    A word appearing in most paths tells you nothing. In this repo "agentpipe"
    is in almost every path, so matching it means nothing at all. A word in one
    path is a strong signal.

    This is the self-maintaining version of a stopword list. A blacklist of
    boring words is always one word out of date and someone has to keep it.
    Rarity computes itself from the repo in front of you.
    """
    n = len(paths) or 1
    df: Counter[str] = Counter()
    for p in paths:
        for w in _words(p):
            df[w] += 1
    return {w: math.log(n / c) for w, c in df.items()}


def select(
    ticket: Ticket,
    repo: Repo,
    max_files: int = 5,
) -> tuple[Candidate, ...]:
    """Choose which files the pack pays for.

    Two paths, and the first one is the important one.

    If the ticket names files, those are the answer. Full stop. A human decided
    already, and word overlap does not get a vote. The first version of this
    function added its own picks on top of the ticket's, and on the first real
    run it matched "repo" in the goal "the file exists at the repo root" against
    repo.py and test_repo.py. Two irrelevant files, 67% of the pack, half the
    cost of the call. The bug was not the matching. It was second-guessing a
    human who already knew.

    Nothing is lost by trusting the ticket, because the pack still carries the
    whole tree for about 39 tokens, and RULES tells the model to say so if it
    needs a file it cannot see. Asking is cheaper than guessing.

    Only when nobody has said anything does the word overlap run, and then it
    weights matches by rarity so that common words carry little.

    There is deliberately no minimum score. A floor was tried and removed: the
    number would have been invented rather than measured, and on a small repo it
    silently selected nothing at all. If the fallback path turns out to pick
    junk, that will show up in the table as a pack that is bigger than it should
    be, and the floor can be set from that number instead of from a hunch.
    """
    available = repo.files()
    available_set = set(available)

    hinted = [h for h in ticket.files_hint if h in available_set]
    if hinted:
        return tuple(
            Candidate(h, 1000.0, "named in ticket") for h in hinted[:max_files]
        )

    idf = _idf(available)
    goal_words = _words(ticket.goal)
    scored: list[Candidate] = []

    for path in available:
        overlap = goal_words & _words(path)
        if not overlap:
            continue
        score = sum(idf.get(w, 0.0) for w in overlap)
        best = sorted(overlap, key=lambda w: -idf.get(w, 0.0))
        scored.append(
            Candidate(path, score, f"path matches: {', '.join(best)}")
        )

    # max_files is a ceiling, not a quota.
    ranked = sorted(scored, key=lambda c: (-c.score, c.path))
    return tuple(ranked[:max_files])


def cost_report(repo: Repo, selected: tuple[Candidate, ...]) -> str:
    """What the tree costs, what the selection costs, what everything costs.

    Exists so the saving is visible rather than asserted. Layer 1's whole claim
    is that choosing beats including, and a claim you cannot see is a slogan.
    """
    tree_t = estimate_tokens(repo.tree())
    sel_t = sum(estimate_tokens(repo.read(c.path)) for c in selected)
    all_t = sum(estimate_tokens(repo.read(p)) for p in repo.files())

    lines = [
        f"tree only          {tree_t:>7,} tokens   ({len(repo.files())} paths)",
        f"selected contents  {sel_t:>7,} tokens   ({len(selected)} files)",
        f"pack total         {tree_t + sel_t:>7,} tokens",
        f"whole repo would be{all_t:>7,} tokens",
    ]
    if all_t:
        saved = 100 * (1 - (tree_t + sel_t) / all_t)
        lines.append(f"saved              {saved:>6.1f}%")
    return "\n".join(lines)
