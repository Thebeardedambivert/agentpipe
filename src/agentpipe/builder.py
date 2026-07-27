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
from agentpipe.repo import Candidate, Repo, estimate_tokens, select
from agentpipe.telemetry import CallRecord, MeteredClient
from agentpipe.ticket import Ticket


# This used to have a derivation. It no longer does, and saying so is the point.
#
# The old reasoning was: RULES asks for the complete contents of every file the
# model changes, so output must be at least as large as what it is rewriting.
# That was genuine arithmetic. It expired when RULES stopped asking for whole
# files (see patch.py for why it had to). Output now scales with the size of the
# *change*, which nothing here can predict.
#
# So this is now a deliberately over-provisioned ceiling, kept rather than
# shrunk because an unused ceiling costs nothing and a too-small one produces an
# empty billed reply. It is a guess wearing no disguise.
#
# What would settle a real number, once there are enough search/replace runs:
#
#   select max(output_tokens), avg(output_tokens) from model_calls
#    where role = 'builder' and status = 'ok';
#
# and the same for role='fixer'. Until then, note that across 118 calls this
# project has never once recorded finish_reason='length', so nothing has ever
# been cut off by a budget of this shape.
REWRITE_HEADROOM = 1.5
REASONING_FLOOR = 2_000


def output_budget(
    repo: Repo,
    selected: tuple[Candidate, ...],
    headroom: float = REWRITE_HEADROOM,
    reasoning_floor: int = REASONING_FLOOR,
) -> int:
    """How many output tokens the model is allowed.

    Exists because not setting one is how you get an empty reply: on a reasoning
    model the thinking eats the whole default allowance and there is nothing
    left to answer with. The call succeeds, is billed, and returns nothing.

    Parameters rather than constants, deliberately. The numbers above are
    hypotheses, and a hypothesis you can pass a different value to is one
    somebody can disprove.
    """
    rewriting = sum(estimate_tokens(repo.read(c.path)) for c in selected)
    return int(rewriting * headroom) + reasoning_floor


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
    max_output_tokens: int | None = None,
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
    budget = max_output_tokens or output_budget(repo, selected)

    record = client.call(
        messages=pack.as_list(),
        model=model,
        role="builder",
        attempt_kind="implement" if feedback is None else "validation_retry",
        attempt_index=attempt,
        task_ref=ticket.ref,
        max_completion_tokens=budget,
    )

    # Takes the repo because a reply now describes a change, and a change has no
    # meaning without the thing it changes.
    edits = parse_edits(record.content, repo)

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
        f"out={u.output_tokens:,} (thinking {u.reasoning_tokens:,}, "
        f"answer {u.answer_tokens:,})",
        f"ratio      {u.ratio:.1f} billed / {u.answer_ratio:.1f} on answer",
        f"finish     {result.record.finish_reason}"
        + ("   <- cut off. Raise --max-output." 
           if result.record.finish_reason == "length" else ""),
        f"cost       ${result.cost_usd}",
        f"status     {result.record.status}",
    ]
    if dry_run:
        lines += ["", "dry run. Nothing was written. Pass --apply to write."]
    return "\n".join(lines)
