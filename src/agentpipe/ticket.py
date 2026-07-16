"""The ticket.

A ticket is not a description of work. It is a contract, and the pipeline
refuses to start without a valid one.

The reason is money. Every downstream cost in this system traces back to how
well-specified the ticket was. A vague goal means the agent guesses. A missing
validation command means nobody can contradict the agent when it says "done".
Absent acceptance criteria, "done" is whatever the model decided it meant.

Rejecting a bad ticket here costs nothing. Discovering it was bad after five
attempts costs five attempts. So this module is deliberately strict, and it is
the only part of the pipeline that is allowed to be picky for free.

Format (markdown, sections by ## heading):

    # TASK-1

    ## Goal
    One sentence describing what is true when this is done.

    ## Validation
    ```
    pytest -q
    ```

    ## Acceptance
    - [ ] Something a human can check
    - [ ] Something else

    ## Constraints
    - Optional. Things the agent must not do.

    ## Files
    - Optional. Hints about where to look.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MIN_GOAL_CHARS = 20


class TicketError(Exception):
    """A ticket that cannot be trusted to produce checkable work.

    Carries every problem at once rather than the first one. Fixing a ticket
    one error at a time is miserable, and misery is how people learn to write
    tickets that technically pass.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"ticket is not valid:\n{joined}")


@dataclass(frozen=True)
class Ticket:
    """A contract the pipeline can act on.

    Frozen because Layer 1's whole promise is determinism: the same ticket must
    always produce the same context pack. A ticket that can be mutated after
    parsing is a ticket that can quietly differ between attempt 1 and attempt 4.
    """

    ref: str
    goal: str
    validation: tuple[str, ...]
    acceptance: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    files_hint: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: str | Path) -> Ticket:
        p = Path(path)
        return cls.parse(p.read_text(encoding="utf-8"), ref_fallback=p.stem)

    @classmethod
    def parse(cls, text: str, ref_fallback: str | None = None) -> Ticket:
        sections = _split_sections(text)
        problems: list[str] = []

        ref = _title(text) or ref_fallback or ""
        if not ref.strip():
            problems.append(
                "no ticket ref. Start the file with a '# TASK-1' heading."
            )

        goal = sections.get("goal", "").strip()
        if not goal:
            problems.append("no '## Goal' section, or it is empty.")
        elif len(goal) < MIN_GOAL_CHARS:
            problems.append(
                f"goal is {len(goal)} characters. That is not a goal, it is a "
                f"title. Say what is true when this is done."
            )

        validation = _commands(sections.get("validation", ""))
        if not validation:
            problems.append(
                "no '## Validation' commands. Without these the agent's claim "
                "that it worked is the only evidence, and that is not evidence."
            )

        acceptance = _bullets(sections.get("acceptance", ""))
        if not acceptance:
            problems.append(
                "no '## Acceptance' criteria. Without these, 'done' means "
                "whatever the model decides it means."
            )

        if problems:
            raise TicketError(problems)

        return cls(
            ref=ref.strip(),
            goal=goal,
            validation=validation,
            acceptance=acceptance,
            constraints=_bullets(sections.get("constraints", "")),
            files_hint=_bullets(sections.get("files", "")),
        )


def _title(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1) if m else ""


def _split_sections(text: str) -> dict[str, str]:
    """Split on '## Heading'. Keys are lowercased headings."""
    out: dict[str, str] = {}
    parts = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        out[parts[i].strip().lower()] = parts[i + 1]
    return out


def _commands(block: str) -> tuple[str, ...]:
    """Pull commands out of fenced code blocks.

    Fenced only, deliberately. A command is a thing that gets executed, and
    execution deserves an explicit marker rather than being inferred from a
    line that happened to look shell-ish.
    """
    fences = re.findall(r"```[a-z]*\n(.*?)```", block, re.DOTALL)
    cmds = [
        line.strip()
        for fence in fences
        for line in fence.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return tuple(cmds)


def _bullets(block: str) -> tuple[str, ...]:
    out = []
    for line in block.splitlines():
        line = line.strip()
        m = re.match(r"^[-*]\s*(?:\[[ xX]\]\s*)?(.+)$", line)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return tuple(out)
