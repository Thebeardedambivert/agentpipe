"""The fixer loop.

Stage 1 gave a reviewer that finds problems and prints them. This is the worker
that acts on them, safely. It reviews, repairs the single worst problem, re-runs
the tests, and keeps the change only if the tests still pass. A repair that breaks
them is reverted, so the loop can only ever leave code better or unchanged, never
worse.

The decisions this rests on, all agreed and locked in plans/layer5.md:

- One finding per round, worst first (Fork 6). A round repairs one problem, then
  re-reviews from the real updated code. This isolates damage (a broken fix reverts
  only itself), and it lets the loop notice findings that interact: fixing one can
  dissolve or reveal another, which a batch fix would never see.

- Revert is an in-memory snapshot, not git (Fork 5). Before the fixer runs, the
  reviewed files' contents are read into memory; a breaking fix is undone by
  writing them back and deleting anything the fix created. The one limitation,
  stated plainly: the snapshot lives in memory, so it does not survive a crash
  mid-fix. That is deliberate. Crash-durability is Layer 7's job, and half of it in
  the wrong layer is worse than an honest gap.

- Rebuild, never accumulate. The fixer sees one finding and the current code, never
  past attempts or the review history. This is the place the studied pipeline went
  quadratic.

- The tests decide, not the model. A fixer that says "fixed" has proven nothing;
  the ticket's validation commands and their exit codes are the only evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from agentpipe.checks import CheckResult, Outcome, run_checks
from agentpipe.config import ModelMap
from agentpipe.pack import Pack, _ticket_block
from agentpipe.patch import PatchError, apply_edits, parse_edits
from agentpipe.repo import Repo, estimate_tokens
from agentpipe.review import (
    Finding,
    ReviewError,
    _SEVERITY_RANK,
    _current_files_block,
    run_review,
)
from agentpipe.telemetry import CallRecord, MeteredClient, pack_hash
from agentpipe.ticket import Ticket

# Stable, never interpolated, cache-friendly, same discipline as pack.RULES and
# review.REVIEW_RULES. The fixer's whole brief: change one thing, change it small,
# keep the tests green.
FIX_RULES = """You are a careful software engineer fixing one specific problem in code that already passes its tests.

A reviewer flagged the single issue below. Make the smallest change that resolves exactly that issue. Do not refactor, do not fix anything else, do not touch files you were not given. The tests must still pass after your change.

Reply with the complete new contents of each file you change, in this format:

--- path/to/file.py
<the full file contents>
--- end

Nothing else. No explanation, no markdown fences around the whole reply."""


def _key(f: Finding) -> tuple:
    """Identity of a finding, so one already tried is not tried again.

    Without this, a reverted or unparseable fix would let the same finding come
    back on the next review (the code is unchanged) and be attempted forever,
    replaying the identical (broken) reply for free but churning rounds to no end.
    """
    return (f.severity, f.file, f.line, f.issue)


def build_fix_pack(
    ticket: Ticket, repo: Repo, files: tuple[str, ...], finding: Finding
) -> Pack:
    """The fixer's context: the ticket, the current code, and the one problem.

    Ordered most-stable-first like pack.build so the cached-input discount applies:
    rules and ticket rarely change, the code changes per round, the specific
    finding is last. No history, by design.
    """
    system = FIX_RULES
    asked = _ticket_block(ticket)
    code = _current_files_block(repo, files)
    where = f"{finding.file}:{finding.line}" if finding.line else finding.file
    problem = (
        "## The one problem to fix\n"
        f"[{finding.severity}] {where}\n{finding.issue}"
    )
    user = "\n\n".join([asked, "## Current code", code, problem])

    messages = (
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    )
    return Pack(
        messages=messages,
        hash=pack_hash(list(messages)),
        tokens=estimate_tokens(system + user),
    )


def _snapshot(repo: Repo, files: tuple[str, ...]) -> dict[str, str]:
    """Capture the current contents of the files that exist.

    A file in `files` that does not exist yet is absent from the snapshot on
    purpose: its "prior state" is "did not exist", and _restore reads that as
    "delete it if the fix created it".
    """
    snap: dict[str, str] = {}
    for p in files:
        if (repo.root / p).exists():
            snap[p] = repo.read(p)
    return snap


def _restore(repo: Repo, files: tuple[str, ...], snapshot: dict[str, str]) -> None:
    """Undo a fix: rewrite captured contents, delete anything it created.

    newline="\\n" matches patch.py, so a revert does not rewrite line endings on
    Windows and turn a clean undo into a whole-file diff.
    """
    for p, text in snapshot.items():
        (repo.root / p).write_text(text, encoding="utf-8", newline="\n")
    for p in files:
        full = repo.root / p
        if p not in snapshot and full.exists():
            full.unlink()


@dataclass(frozen=True)
class RoundResult:
    """One round: which finding, what happened, and the proof."""

    finding: Finding
    outcome: str  # fixed | reverted | unfixable
    fix_record: CallRecord
    validation: tuple[CheckResult, ...]


@dataclass(frozen=True)
class ReviewFixResult:
    rounds: tuple[RoundResult, ...]
    review_records: tuple[CallRecord, ...]  # one per review round
    stopped: str  # clean | settled | max_rounds | review_unparseable

    @property
    def total_cost_usd(self) -> Decimal:
        review = sum((r.cost_usd for r in self.review_records), Decimal(0))
        fix = sum((rd.fix_record.cost_usd for rd in self.rounds), Decimal(0))
        return review + fix

    @property
    def fixed(self) -> tuple[RoundResult, ...]:
        return tuple(rd for rd in self.rounds if rd.outcome == "fixed")

    @property
    def reverted(self) -> tuple[RoundResult, ...]:
        return tuple(rd for rd in self.rounds if rd.outcome == "reverted")


def run_review_fix(
    ticket: Ticket,
    repo: Repo,
    client: MeteredClient,
    models: ModelMap,
    files: tuple[str, ...],
    max_rounds: int = 3,
    min_severity: str = "medium",
) -> ReviewFixResult:
    """Review, fix the worst problem, re-validate, keep or revert. Repeat.

    `min_severity` defaults to "medium": acting on every "low" nitpick costs money
    and churns the diff for little gain. It is a labelled guess, not a tuned
    constant. What would settle it: Stage 3's review_findings table, which records
    which severities' fixes actually survive re-validation. `max_rounds` is the same
    kind of guess, and the hard stop that bounds cost when a finding never clears.

    Writes to the working tree (the fixer edits real files that validation runs
    against), so run it on a scratch repo or a clean git state you can reset.
    """
    threshold = _SEVERITY_RANK[min_severity]
    reviewer_model = models.for_role("reviewer")
    fixer_model = models.for_role("fixer")

    review_records: list[CallRecord] = []
    rounds: list[RoundResult] = []
    tried: set[tuple] = set()
    stopped = "max_rounds"

    for rnd in range(1, max_rounds + 1):
        # Review the code as it is right now. min_severity="low" so we see every
        # finding and do the threshold filtering here, which lets us tell "clean"
        # (nothing at all) from "settled" (nothing left worth acting on).
        try:
            review = run_review(
                ticket, repo, client, reviewer_model, files, min_severity="low"
            )
        except ReviewError:
            # A malformed review is not retried: the call is recorded, so an
            # identical retry would replay the identical bad reply for free and
            # fail the same way. Stop honestly instead of spinning.
            stopped = "review_unparseable"
            break

        review_records.append(review.record)

        actionable = [
            f for f in review.findings
            if f.rank <= threshold and _key(f) not in tried
        ]
        if not actionable:
            stopped = "clean" if not review.findings else "settled"
            break

        finding = actionable[0]  # findings come ranked worst-first
        tried.add(_key(finding))

        snapshot = _snapshot(repo, files)

        fix_record = client.call(
            messages=build_fix_pack(ticket, repo, files, finding).as_list(),
            model=fixer_model,
            role="fixer",
            attempt_kind="review_fix",
            attempt_index=rnd,
            task_ref=ticket.ref,
        )

        try:
            edits = parse_edits(fix_record.content)
            # allowed = the reviewed files only, so the fixer cannot wander outside
            # what we snapshotted and leave a change the revert cannot undo.
            apply_edits(repo, edits, allowed=files, dry_run=False)
        except PatchError:
            rounds.append(RoundResult(finding, "unfixable", fix_record, ()))
            continue

        results = run_checks(ticket.validation, repo.root)
        if all(r.outcome is Outcome.PASS for r in results):
            rounds.append(RoundResult(finding, "fixed", fix_record, results))
        else:
            _restore(repo, files, snapshot)
            rounds.append(RoundResult(finding, "reverted", fix_record, results))

    return ReviewFixResult(tuple(rounds), tuple(review_records), stopped)


def report_review_fix(result: ReviewFixResult) -> str:
    """The whole review-fix run in one block a human can read."""
    headline = {
        "clean": "CLEAN (nothing left to fix)",
        "settled": "SETTLED (remaining findings could not be fixed)",
        "max_rounds": "STOPPED (hit the round limit)",
        "review_unparseable": "STOPPED (the reviewer's reply was unusable)",
    }.get(result.stopped, result.stopped.upper())

    lines = [
        f"review-fix {headline}",
        f"rounds     {len(result.rounds)}",
        f"cost       ${result.total_cost_usd}",
    ]
    for i, rd in enumerate(result.rounds, start=1):
        where = f"{rd.finding.file}:{rd.finding.line}" if rd.finding.line else rd.finding.file
        u = rd.fix_record.usage
        lines.append(
            f"  round {i}: {rd.outcome:<9} [{rd.finding.severity}] {where}  "
            f"in={u.input_tokens:,} out={u.output_tokens:,} ${rd.fix_record.cost_usd}"
        )
        lines.append(f"            {rd.finding.issue}")

    if result.reverted:
        lines += ["", "reverted fixes broke validation and were undone; "
                  "the code is unchanged where they ran."]

    return "\n".join(lines)
