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
    - [ ] Something a machine can check `check: python -c "..."`

    ## Constraints
    - Optional. Things the agent must not do.

    ## Files
    - Optional. Hints about where to look.

An acceptance bullet may carry an inline `check:` command. Its exit code is read
before any model is called: 0 means the work is already present, 1 means it is
not, anything else means the check itself is broken. A ticket whose checks all
pass is already done, so the pipeline says so and stops, for free, rather than
paying to rewrite a correct file. Checks are optional and live on the bullet they
verify, on purpose: a check floating in its own section drifts from the criterion
it was meant to prove, and two specs of "done" that disagree is the exact bug
this project keeps paying for. See checks.py for the exit-code contract and the
trust boundary these commands run under.
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
class Criterion:
    """One acceptance criterion, and optionally how a machine checks it.

    `text` is for the human and for the model. `check` is a command whose exit
    code says whether this criterion already holds. It lives on the criterion,
    not in a separate list, so the two cannot drift apart.
    """

    text: str
    check: str | None = None


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
    acceptance: tuple[Criterion, ...]
    constraints: tuple[str, ...] = ()
    files_hint: tuple[str, ...] = ()

    @property
    def checks(self) -> tuple[str, ...]:
        """The check commands, for the criteria that carry one.

        A ticket with no checks returns an empty tuple, which the staleness gate
        reads as "unguarded", never as "already done". Absence of a check is not
        evidence the work exists.
        """
        return tuple(c.check for c in self.acceptance if c.check)

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

        acceptance = _criteria(sections.get("acceptance", ""))
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


def _criteria(block: str) -> tuple[Criterion, ...]:
    """Acceptance bullets, each with an optional trailing `check: ...` command.

    The check is pulled off the end of the bullet and stored on the same
    Criterion as the text it verifies. Everything before the check is the human
    text. A bullet with no check is a criterion a human reads and a machine
    cannot yet judge, which is fine: not every criterion reduces to an exit code.
    """
    out = []
    for line in block.splitlines():
        m = re.match(r"^[-*]\s*(?:\[[ xX]\]\s*)?(.+)$", line.strip())
        if not (m and m.group(1).strip()):
            continue
        body = m.group(1).strip()
        check = None
        cm = re.search(r"`check:\s*(.+?)`\s*$", body)
        if cm:
            check = cm.group(1).strip()
            body = body[: cm.start()].strip()
        if body:
            out.append(Criterion(text=body, check=check))
    return tuple(out)
