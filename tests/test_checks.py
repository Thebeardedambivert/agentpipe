"""Tests for the staleness gate.

The gate exists to answer one question before any money is spent: is this
ticket's work already done? These tests pin the three outcomes it must
distinguish, and in particular the one a naive version gets wrong: a check that
cannot run is not the same as a check that says "not done".

Commands are written as `python -c "..."` so they run identically on the Windows
dev machine and on Linux CI. A bash-ism here would pass locally and error in CI,
which is the exact divergence the gate is supposed to catch, so the tests must
not commit it either.
"""

from __future__ import annotations

from agentpipe.checks import Outcome, Verdict, assess, run_check
from agentpipe.ticket import Criterion, Ticket


def _ticket(*criteria: Criterion) -> Ticket:
    """A minimal valid-shaped ticket carrying the given criteria.

    Built directly rather than parsed: these tests are about the gate, not the
    parser, and the parser has its own tests.
    """
    return Ticket(
        ref="TASK-TEST",
        goal="x" * 25,
        validation=("pytest -q",),
        acceptance=criteria,
    )


def _exists(name: str) -> str:
    return f'python -c "import os, sys; sys.exit(0 if os.path.exists(\'{name}\') else 1)"'


def test_all_checks_pass_means_satisfied(tmp_path):
    """The work is already on disk, so the gate stops the run for free."""
    (tmp_path / "done.txt").write_text("x", encoding="utf-8")
    t = _ticket(Criterion("done.txt exists", _exists("done.txt")))
    decision = assess(t, tmp_path)
    assert decision.verdict is Verdict.SATISFIED


def test_a_failing_check_means_proceed(tmp_path):
    """The file is missing, so there is real work; exit 1 is a genuine answer."""
    t = _ticket(Criterion("missing.txt exists", _exists("missing.txt")))
    decision = assess(t, tmp_path)
    assert decision.verdict is Verdict.PROCEED


def test_a_ticket_with_no_checks_is_unguarded_not_satisfied(tmp_path):
    """Absence of a check is not evidence the work exists.

    A ticket whose only criterion is prose has nothing to run, so the gate must
    build, not declare victory. Declaring victory here would be the stale-ticket
    hole reopening from the other side.
    """
    t = _ticket(Criterion("the file explains itself to a reader", None))
    decision = assess(t, tmp_path)
    assert decision.verdict is Verdict.PROCEED
    assert "unguarded" in decision.reason


def test_a_broken_check_is_loud_not_treated_as_unfinished(tmp_path):
    """The whole reason the gate has three states instead of two.

    Exit 2 is not "the work is not done". It is "this check does not work". A gate
    that read any non-zero as failure would silently send the pipeline off to
    spend money because someone fat-fingered a check command. That is precisely
    the shape of every silent failure in this project: a broken instrument read
    as a valid measurement.
    """
    t = _ticket(Criterion("broken", 'python -c "import sys; sys.exit(2)"'))
    decision = assess(t, tmp_path)
    assert decision.verdict is Verdict.BROKEN


def test_a_missing_command_is_never_read_as_satisfied(tmp_path):
    """A command that does not exist must never look like "the work is done".

    Its exit code is platform-dependent and this bit us: POSIX shells return 127
    (-> ERROR), but Windows cmd.exe returns 1 (-> FAIL), the same code as "work
    not done". So we do not pin the exact verdict. We pin the property that
    actually matters and holds everywhere: a broken or missing check never passes
    the gate as satisfied. On Windows the cost is a needless build; on POSIX it is
    a loud stop. Neither silently skips the work, which is the only unacceptable
    outcome.
    """
    result = run_check("definitely-not-a-real-command-xyz", tmp_path)
    assert result.outcome in (Outcome.ERROR, Outcome.FAIL)
    assert result.outcome is not Outcome.PASS


def test_exit_zero_one_and_two_map_to_pass_fail_error(tmp_path):
    """The exit-code contract, spelled out as a test so it cannot drift."""
    assert run_check('python -c "import sys; sys.exit(0)"', tmp_path).outcome is Outcome.PASS
    assert run_check('python -c "import sys; sys.exit(1)"', tmp_path).outcome is Outcome.FAIL
    assert run_check('python -c "import sys; sys.exit(2)"', tmp_path).outcome is Outcome.ERROR
