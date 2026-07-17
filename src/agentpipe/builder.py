"""The builder.

Ticket in, files on disk out. One shot, no loop, no validation.

Nothing new is invented here. Every piece already exists and this is only the
wiring:

    ticket   -> what "done" means, and how it will be checked
    select   -> which files are worth paying for
    build    -> the pack, ordered so the cache discount applies
    client   -> the one door to a model, which prices the call
    parse    -> the reply, refused unless unambiguous
    apply    -> the disk, refused unless safe

Deliberately no loop. A loop hides bugs: if the one-shot case is wrong, the
looped version is wrong five times and much harder to read. Layer 3 adds the
loop, once this is boring.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentpipe.pack import build
from agentpipe.patch import FileEdit, apply_edits, parse_edits, summarise
from agentpipe.repo import Candidate, Repo, select
from agentpipe.telemetry import CallRecord, MeteredClient
from agentpipe.ticket import Ticket


@dataclass(frozen=True)
class BuildResult:
    ticket_ref: str
    pack_hash: str
    pack_tokens: int
    selected: tuple[Candidate, ...]
    edits: tuple[FileEdit, ...]
    written: tuple[str, ...]
    record: CallRecord

    @property
    def ratio(self) -> float:
        return self.record.usage.ratio

    @property
    def cost_usd(self):
        return self.record.cost_usd


def run_builder(
    ticket: Ticket,
    repo: Repo,
    client: MeteredClient,
    model: str,
    attempt: int = 1,
    feedback: str | None = None,
    max_files: int = 5,
    dry_run: bool = True,
) -> BuildResult:
    """One attempt at a ticket.

    `dry_run` defaults to True. Writing to someone's working tree is not a
    thing to do by accident, and the interesting output of this function (the
    pack, the cost, the ratio, what it would change) is available without
    touching a single file. Turning writing on should be a decision.

    `feedback` and `attempt` exist for Layer 3 and are unused today. They are in
    the signature now because adding them later would change every call site,
    and because they document what varies between attempts: the feedback, and
    nothing else. Not a history. Not a transcript. One string, appended last.
    """
    selected = select(ticket, repo, max_files=max_files)
    pack = build(ticket, repo, selected, feedback=feedback)

    record = client.call(
        messages=pack.as_list(),
        model=model,
        role="builder",
        attempt_kind="implement" if feedback is None else "validation_retry",
        attempt_index=attempt,
        task_ref=ticket.ref,
    )

    edits = parse_edits(record.content)

    # The ticket named the files it expected to change. Anything outside that
    # set is not necessarily wrong, but it was not agreed, and a human should
    # see it before it lands. When the ticket named nothing, we have no agreed
    # set to enforce, so the repo boundary is the only guard.
    allowed = ticket.files_hint or None
    written = apply_edits(repo, edits, allowed=allowed, dry_run=dry_run)

    return BuildResult(
        ticket_ref=ticket.ref,
        pack_hash=pack.hash,
        pack_tokens=pack.tokens,
        selected=selected,
        edits=edits,
        written=written,
        record=record,
    )


def report(result: BuildResult, repo: Repo, dry_run: bool) -> str:
    """What happened, and what it cost, in one block a human can read."""
    lines = [
        f"ticket     {result.ticket_ref}",
        f"pack       {result.pack_tokens:,} tokens est, hash {result.pack_hash}",
        "",
        "selected:",
    ]
    for c in result.selected:
        lines.append(f"  {c.path:<32} {c.reason}")

    lines += ["", "would change:" if dry_run else "changed:"]
    lines.append(summarise(result.edits, repo))

    u = result.record.usage
    lines += [
        "",
        f"actual     in={u.input_tokens:,} (cached {u.cached_input_tokens:,}) "
        f"out={u.output_tokens:,}",
        f"ratio      {u.ratio:.1f}",
        f"cost       ${result.cost_usd}",
        f"status     {result.record.status}",
    ]
    if dry_run:
        lines += ["", "dry run. Nothing was written. Pass --apply to write."]
    return "\n".join(lines)
