"""Executable acceptance checks: is this ticket's work already present?

A ticket's ## Validation commands answer "is the repo healthy?" (pytest passes).
Its acceptance *checks* answer a different question: "is THIS ticket's specific
work present right now?" Run them before spending a model call, and a ticket
whose work is already done stops for free, instead of paying to rewrite a correct
file. That waste, a stale ticket the pipeline dutifully "completes", is the whole
reason this module exists.

Exit-code contract, and it is load-bearing:

    0              the work is present         -> pass
    1              the work is not present     -> fail (there is something to do)
    anything else  the check itself is broken  -> error (not an answer)

The third line is the point. "The command exited non-zero" is not one fact, it
is two. "The work is not done" and "your check does not run" are different
problems, and conflating them is how every silent failure in this project
started. A broken check must stop the pipeline loudly, not quietly wave it
through as though the work simply was not done. This mirrors common tools: grep
and pytest both use 0 / 1 / higher the same way.

The reliable broken-signal is exit code 2 or higher (or a timeout, or a shell
that cannot launch at all). There is one platform seam, found the hard way: a
command that does not *exist* exits 127 on POSIX (-> error, caught) but 1 on
Windows cmd.exe (-> fail, not caught), the same code as "work not done". So a
typo'd check command is loudly caught in CI and on Linux, but on the Windows dev
machine it reads as unfinished work and triggers a needless build instead of a
stop. That is the safe direction (it never reads as "already done"), but it is a
real limit: rely on exit >= 2 for "broken", not on a missing command's code.

Trust boundary, stated so it is not merely assumed: these commands run with your
full privileges through the platform shell. A ticket is only text, and running
its checks runs its code. That is fine while you write your own tickets. It is
not fine for a ticket from anywhere you do not control, and the fix then is a
sandbox, not a promise. Checks must also be read-only probes: the gate runs one
before the work, Layer 3 will run it again to confirm, and the loop runs it every
attempt, so a check with side effects corrupts the very thing it measures, which
is the "tests wrote fixture rows into the live table" bug in a new costume.

Portable only. Commands run through the platform's default shell (cmd.exe on
Windows, sh on Linux), so write them as `python -c "..."` or cross-platform
tools, never bash-isms. A check that passes on your laptop and errors in CI is
the test-double-disagrees-with-the-real-thing bug wearing a shell.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentpipe.ticket import Ticket

# A ceiling, not a tuned number. A check is meant to be a quick probe; one that
# runs longer than this is either doing real work (a side effect, which is
# forbidden) or hung. Nothing derives this value. Raise it if a legitimate check
# ever needs longer, and if that happens often, that is evidence the check
# design is wrong, not that the ceiling is.
CHECK_TIMEOUT_SECONDS = 30


class Outcome(Enum):
    PASS = "pass"    # exit 0: the work is present
    FAIL = "fail"    # exit 1: the work is not present, there is something to do
    ERROR = "error"  # anything else, or could not launch: the check did not run


@dataclass(frozen=True)
class CheckResult:
    command: str
    outcome: Outcome
    exit_code: int | None  # None when the shell could not launch the command
    output: str


def run_check(command: str, cwd: Path) -> CheckResult:
    """Run one check command in `cwd` and classify its exit code.

    Note the three branches on returncode. A naive gate would treat any non-zero
    as "failed", which silently turns a typo in a check command into a false
    "there is work to do" and sends the pipeline off to spend money. Exit 1 is an
    answer; anything else is a broken instrument.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            check=False,  # the exit code is the answer; we classify it, never raise on it
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(command, Outcome.ERROR, None, str(exc))

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        outcome = Outcome.PASS
    elif proc.returncode == 1:
        outcome = Outcome.FAIL
    else:
        outcome = Outcome.ERROR
    return CheckResult(command, outcome, proc.returncode, output)


def run_checks(commands: tuple[str, ...], cwd: str | Path) -> tuple[CheckResult, ...]:
    root = Path(cwd)
    return tuple(run_check(c, root) for c in commands)


class Verdict(Enum):
    SATISFIED = "satisfied"  # every check passed: the work is already done
    PROCEED = "proceed"      # go build: a check failed, or there were no checks
    BROKEN = "broken"        # a check could not run: stop and fix the ticket


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    results: tuple[CheckResult, ...]
    reason: str


def assess(ticket: Ticket, cwd: str | Path) -> GateDecision:
    """Decide, before any model call, whether this ticket's work already exists.

    Three outcomes, never two. SATISFIED stops the run as a success, nothing to
    do. PROCEED builds. BROKEN stops the run as a failure, because a check that
    cannot run is a broken ticket, not finished work, and pretending otherwise is
    the silent failure this whole design is trying to refuse.
    """
    checks = ticket.checks
    if not checks:
        return GateDecision(
            Verdict.PROCEED,
            (),
            "no executable checks on this ticket, so it is unguarded; building anyway",
        )

    results = run_checks(checks, cwd)

    if any(r.outcome is Outcome.ERROR for r in results):
        return GateDecision(
            Verdict.BROKEN,
            results,
            "a check could not run; that is a broken ticket, not unfinished work",
        )
    if all(r.outcome is Outcome.PASS for r in results):
        return GateDecision(
            Verdict.SATISFIED,
            results,
            "every acceptance check already passes; there is nothing to do",
        )
    return GateDecision(
        Verdict.PROCEED,
        results,
        "some acceptance checks report work remaining",
    )
