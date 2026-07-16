"""The pack.

Everything the model will ever see, assembled deliberately.

The interesting decision here is not what goes in. It is the order.

Cached input costs a tenth of fresh input, but caching only applies to the
*prefix* of a prompt, and only while that prefix is byte-identical to last time.
One changed character and everything after it is billed at full price.

So the pack is sorted by volatility, most stable first:

    1. rules       identical on every call, forever
    2. tree        same for every ticket at this commit
    3. files       changes per ticket
    4. ticket      changes per ticket
    5. feedback    changes every attempt          (Layer 3 fills this in)

Attempts 2 through 5 change only the bottom. Everything above it stays cached,
at 10% of price. Order the pack the other way round, put the attempt number
first, and you pay full freight on every token of every attempt.

That is the whole reason Layer 1 exists before Layer 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentpipe.repo import Candidate, Repo, estimate_tokens
from agentpipe.telemetry import pack_hash
from agentpipe.ticket import Ticket

# Stable, boring, and never interpolated. Every character here is one the cache
# can reuse forever. Resist the urge to put the ticket ref or a timestamp in it:
# that would move the volatile part to the front and silently cost 10x.
RULES = """You are a careful software engineer working in an existing repository.

Rules:
- Make the smallest change that satisfies the goal.
- Do not touch files outside the ones you were given.
- Do not invent APIs. If you need something you cannot see, say so instead.
- Follow the conventions already visible in the code.
- Your claim that the work is done is not evidence. The validation commands
  decide. Write code that passes them.

Reply with the complete new contents of each file you change, in this format:

--- path/to/file.py
<the full file contents>
--- end

Nothing else. No explanation, no markdown fences around the whole reply."""


@dataclass(frozen=True)
class Pack:
    """A built context pack, ready to send, and priced before it is sent."""

    messages: tuple[dict[str, Any], ...]
    hash: str
    tokens: int

    def as_list(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self.messages]


def _files_block(repo: Repo, selected: tuple[Candidate, ...]) -> str:
    if not selected:
        return "(no files selected)"
    chunks = []
    for c in selected:
        chunks.append(f"--- {c.path}\n{repo.read(c.path)}\n--- end")
    return "\n\n".join(chunks)


def _ticket_block(ticket: Ticket) -> str:
    parts = [f"# {ticket.ref}", "", "## Goal", ticket.goal]

    if ticket.constraints:
        parts += ["", "## Constraints"]
        parts += [f"- {c}" for c in ticket.constraints]

    parts += ["", "## Acceptance criteria"]
    parts += [f"- {a}" for a in ticket.acceptance]

    parts += ["", "## These commands decide whether you succeeded"]
    parts += [f"- {v}" for v in ticket.validation]

    return "\n".join(parts)


def build(
    ticket: Ticket,
    repo: Repo,
    selected: tuple[Candidate, ...],
    feedback: str | None = None,
) -> Pack:
    """Assemble the pack.

    A pure function. Same ticket, same repo state, same selection, same
    feedback produces a byte-identical pack and therefore an identical hash.

    That is not a nice property, it is the load-bearing one. The hash is what
    makes idempotency work in Layer 0, what makes replay possible in Layer 4,
    and what makes the cache discount real. Break determinism here and three
    layers quietly stop working while continuing to look fine.

    `feedback` is the Layer 3 hook. Note that it goes last and that it is the
    only thing that changes between attempts. Note also what is NOT a parameter:
    previous attempts. Attempt 4 does not receive attempts 1 through 3. It
    receives the repository as it is now. That is the entire fix for the 70k
    problem, and it is enforced by this signature.
    """
    system = RULES
    tree = f"Files in this repository:\n\n{repo.tree()}"
    files = _files_block(repo, selected)
    ticket_text = _ticket_block(ticket)

    user_parts = [tree, files, ticket_text]
    if feedback:
        user_parts.append(f"## The last attempt failed validation\n\n{feedback}")

    messages = (
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    )

    return Pack(
        messages=messages,
        hash=pack_hash(list(messages)),
        tokens=estimate_tokens(system + "".join(user_parts)),
    )
