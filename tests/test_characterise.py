"""Characterisation tests: the L4 sensor.

Built after funcy #108 produced a patch that passed 205 tests, did not fix the
reported bug, and quietly broke something else. Every test here is about the
question nothing in the pipeline was asking: did anything change that nobody
asked for?

The real-repository proof lives in test_the_funcy_regression_is_caught, which
builds a miniature of the actual defect rather than mocking the recorder. A test
double that agrees with itself is this project's most expensive recurring bug.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from agentpipe.characterise import (
    CharacteriseError,
    Difference,
    Observation,
    compare,
    modules_for,
    record,
    report,
)

# A library with a function whose answer depends on a detail a patch can plausibly
# get wrong, plus a test that passes either way. That combination is the whole
# point: green tests over changed behaviour.
LIB_BEFORE = '''\
def spec(cls):
    """Argument names, minus the first parameter of __init__."""
    first = cls.__init__.__code__.co_varnames[0]
    names = set(cls.__init__.__code__.co_varnames[:cls.__init__.__code__.co_argcount])
    return sorted(names - {first})


def unrelated(x):
    return x * 2
'''

# The same shape of mistake agentpipe made in funcy: the name of __init__ rather
# than the name of __init__'s first parameter.
LIB_AFTER = LIB_BEFORE.replace(
    "    first = cls.__init__.__code__.co_varnames[0]\n",
    "    first = getattr(cls.__init__, '__name__', None)\n",
)

# Passes against both versions, exactly like funcy's 205 did.
TESTS = '''\
from lib import spec, unrelated


class Thing:
    def __init__(self, a, b=2):
        pass


def test_spec_mentions_the_real_arguments():
    got = spec(Thing)
    assert "a" in got and "b" in got


def test_unrelated_still_doubles():
    assert unrelated(3) == 6
'''


@pytest.fixture
def library(tmp_path):
    """A tiny installable-by-path project with its own passing test suite."""
    (tmp_path / "lib.py").write_text(LIB_BEFORE, newline="\n")
    (tmp_path / "test_lib.py").write_text(TESTS, newline="\n")
    return tmp_path


def run_suite(root):
    """The suite itself passes. Asserted, so a broken fixture cannot masquerade."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(root)],
        cwd=root, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


COMMAND = f'"{sys.executable}" -m pytest -q'


# --- the sensor, on a real subprocess ---------------------------------------

def test_the_funcy_regression_is_caught(library):
    """Named for the run that made this module necessary.

    funcy #108, 27 July 2026, gpt-5.4-mini, $0.004744. The patch passed all 205
    of funcy's tests, left rcurry(str.endswith) raising the same ValueError it was
    asked to fix, and replaced the name of __init__'s first parameter with
    __init__.__name__, so 'self' stopped being stripped from every class's
    argument spec:

        before : names={'a', 'b'},         req_names={'a'}
        after  : names={'a', 'self', 'b'}, req_names={'a', 'self'}

    Validation could not see it, the acceptance check could not see it, and the
    judge reads code rather than running it. A human reading the diff was the only
    thing that caught it. This test is that human.

    The mistake is reproduced in miniature rather than mocked, and the suite is
    asserted green in both states first, because "the tests would have caught it"
    is precisely the claim under test.
    """
    code, output = run_suite(library)
    assert code == 0, f"the fixture suite must pass before the patch:\n{output}"
    before = record(library, ("lib",), COMMAND)

    (library / "lib.py").write_text(LIB_AFTER, newline="\n")

    code, output = run_suite(library)
    assert code == 0, f"the tests must still pass after it, or this proves nothing:\n{output}"
    after = record(library, ("lib",), COMMAND)

    differences = compare(before, after)

    assert differences, "the regression went unnoticed, which is the bug this module exists for"
    assert len(differences) == 1, f"expected only spec() to change, got {differences}"
    assert differences[0].func == "lib.spec"
    assert "'a', 'b'" in differences[0].before
    assert "self" in differences[0].after


def test_an_unchanged_library_reports_no_differences(library):
    """The other half. A sensor that always fires is not a sensor."""
    before = record(library, ("lib",), COMMAND)
    after = record(library, ("lib",), COMMAND)
    assert compare(before, after) == ()


def test_records_what_the_suite_actually_calls(library):
    """The measurement this module was designed around, pinned.

    The design rests on the repo's own suite already supplying the revealing
    input, which was measured on funcy (32 calls to get_spec, 6 with a class)
    rather than assumed. If recording ever silently stops capturing calls, that
    assumption fails quietly and every comparison reports 'nothing changed'.
    """
    observed = record(library, ("lib",), COMMAND)
    functions = {o.func for o in observed}
    assert functions == {"lib.spec", "lib.unrelated"}


def test_a_module_that_cannot_be_watched_is_not_a_clean_snapshot(library):
    """Named for this module's own first live run, which gave a false all-clear.

    27 July 2026, funcy #108 with --characterise. The ticket's validation command
    is a bare `pytest -q`, which does not put the repository on sys.path, where
    `python -m pytest` does. The plugin's import of funcy._inspect failed, the
    failure was swallowed with a `continue`, nothing was instrumented, and the
    report read:

        characterise: no behaviour changed across 0 recorded calls

    A sensor announcing all-clear while switched off is worse than no sensor.
    Both the never-instrumented case and the nothing-recorded case now raise.
    """
    with pytest.raises(CharacteriseError, match="could be watched|not a clean snapshot"):
        record(library, ("no_such_module_anywhere",), COMMAND)


def test_a_watched_module_the_suite_never_calls_is_refused_too(library):
    """Watched but never exercised is not evidence of anything either."""
    (library / "untouched.py").write_text("def never_called():\n    return 1\n",
                                          newline="\n")
    with pytest.raises(CharacteriseError, match="recorded no calls"):
        record(library, ("untouched",), COMMAND)


def test_the_repo_is_importable_even_under_a_bare_pytest(library):
    """The actual fix: the plugin puts the rootdir on sys.path itself.

    Recording with bare `pytest` rather than `python -m pytest` is what broke it
    live, so that is the invocation this asserts.
    """
    observed = record(library, ("lib",), "pytest -q")
    assert {o.func for o in observed} == {"lib.spec", "lib.unrelated"}


def test_a_non_pytest_command_refuses_instead_of_pretending(library):
    """The recorder is a pytest plugin. Anything else must not look like a pass."""
    with pytest.raises(CharacteriseError, match="not pytest"):
        record(library, ("lib",), "make test")


def test_a_command_that_cannot_run_raises_rather_than_reporting_nothing(library):
    """An empty snapshot and a failed snapshot must never look the same.

    A caller that only asks 'were there differences?' would read a failed run as
    a clean bill of health. That is the shape of every silent failure in this
    project, so it raises.
    """
    # Names pytest, so it passes the "is this pytest at all" guard, and then
    # fails to launch. That is the path where the recorder writes nothing.
    with pytest.raises(CharacteriseError, match="never wrote anything"):
        record(library, ("lib",), "pytest-that-does-not-exist -q")


# --- comparison logic, no subprocess needed ---------------------------------

def test_a_call_only_one_run_makes_is_not_a_difference():
    """Different test paths are noise. Only shared calls can disagree."""
    before = (Observation("m.f", "1", "one"),)
    after = (Observation("m.f", "1", "one"), Observation("m.f", "2", "two"))
    assert compare(before, after) == ()


def test_a_raised_exception_is_behaviour_too():
    """A patch that turns an answer into an exception has changed behaviour."""
    before = (Observation("m.f", "x", "'ok'"),)
    after = (Observation("m.f", "x", "raised ValueError: nope"),)
    diffs = compare(before, after)
    assert len(diffs) == 1
    assert "raised ValueError" in diffs[0].after


def test_repeated_calls_compare_first_against_first():
    """A function called twice with the same input must not create a phantom diff."""
    before = (Observation("m.f", "x", "1"), Observation("m.f", "x", "1"))
    after = (Observation("m.f", "x", "1"), Observation("m.f", "x", "1"))
    assert compare(before, after) == ()


# --- reporting and path mapping ---------------------------------------------

def test_the_report_says_how_little_it_looked_at():
    """No differences is not a proof, and the wording must not imply one."""
    text = report((), recorded=32)
    assert "32" in text
    assert "only what the suite already exercises" in text


def test_the_report_names_the_change_and_both_sides():
    text = report((Difference("m.f", "Thing", "{'a'}", "{'a', 'self'}"),), recorded=9)
    assert "m.f(Thing)" in text
    assert "{'a'}" in text and "{'a', 'self'}" in text
    assert "collateral" in text


def test_paths_become_module_names_including_private_ones():
    """funcy/_inspect.py is where the regression lived. Skipping it would have
    missed the only case this module exists for."""
    assert modules_for(("funcy/_inspect.py", "funcy/funcs.py")) == (
        "funcy._inspect", "funcy.funcs",
    )
    assert modules_for(("toolz/__init__.py",)) == ("toolz",)
    assert modules_for(("README.md",)) == ()
