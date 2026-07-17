"""Ticket tests.

Mostly about what it refuses. A parser that accepts everything is not a
contract, it is a formality.
"""

from __future__ import annotations

import pytest

from agentpipe.ticket import Ticket, TicketError

GOOD = """# TASK-1

## Goal
The prices.example.json file exists in the repo root so the setup steps work.

## Validation
```
pytest -q
```

## Acceptance
- [ ] prices.example.json exists at the repo root
- [ ] It contains null values, not real prices

## Constraints
- Do not look up real prices

## Files
- prices.example.json
- README.md
"""


def test_parses_a_good_ticket():
    t = Ticket.parse(GOOD)
    assert t.ref == "TASK-1"
    assert t.goal.startswith("The prices.example.json file exists")
    assert t.validation == ("pytest -q",)
    assert len(t.acceptance) == 2
    assert t.constraints == ("Do not look up real prices",)
    assert t.files_hint == ("prices.example.json", "README.md")


def test_checkbox_and_dash_bullets_both_work():
    t = Ticket.parse(GOOD)
    assert t.acceptance[0].text == "prices.example.json exists at the repo root"
    assert t.acceptance[0].check is None


def test_inline_check_is_parsed_and_split_from_text():
    text = GOOD.replace(
        "- [ ] prices.example.json exists at the repo root",
        '- [ ] prices.example.json exists at the repo root '
        '`check: python -c "import sys; sys.exit(0)"`',
    )
    t = Ticket.parse(text)
    # The check comes off the end; the human text is what remains.
    assert t.acceptance[0].text == "prices.example.json exists at the repo root"
    assert t.acceptance[0].check == 'python -c "import sys; sys.exit(0)"'
    # The other bullet has no check, and that is allowed.
    assert t.acceptance[1].check is None
    # The convenience view the gate uses: only the criteria that carry a check.
    assert t.checks == ('python -c "import sys; sys.exit(0)"',)


def test_multiple_validation_commands():
    text = GOOD.replace("```\npytest -q\n```", "```\npytest -q\nruff check .\n```")
    assert Ticket.parse(text).validation == ("pytest -q", "ruff check .")


def test_comments_are_not_commands():
    text = GOOD.replace("```\npytest -q\n```", "```\n# run the tests\npytest -q\n```")
    assert Ticket.parse(text).validation == ("pytest -q",)


# --- what it refuses ------------------------------------------------------

def test_no_validation_is_refused():
    text = GOOD.replace("## Validation\n```\npytest -q\n```\n", "")
    with pytest.raises(TicketError, match="Validation"):
        Ticket.parse(text)


def test_no_acceptance_is_refused():
    text = GOOD.split("## Acceptance")[0]
    with pytest.raises(TicketError, match="Acceptance"):
        Ticket.parse(text)


def test_no_goal_is_refused():
    text = GOOD.replace(
        "## Goal\nThe prices.example.json file exists in the repo root so the setup steps work.\n",
        "",
    )
    with pytest.raises(TicketError, match="Goal"):
        Ticket.parse(text)


def test_a_title_is_not_a_goal():
    text = GOOD.replace(
        "The prices.example.json file exists in the repo root so the setup steps work.",
        "Add prices file",
    )
    with pytest.raises(TicketError, match="not a goal, it is a title"):
        Ticket.parse(text)


def test_unfenced_commands_do_not_count():
    """A command gets executed. That deserves an explicit marker."""
    text = GOOD.replace("## Validation\n```\npytest -q\n```", "## Validation\npytest -q")
    with pytest.raises(TicketError, match="Validation"):
        Ticket.parse(text)


def test_all_problems_reported_at_once():
    """Not one at a time. Fixing tickets serially teaches people to game them."""
    with pytest.raises(TicketError) as exc:
        Ticket.parse("# TASK-9\n\n## Goal\nx\n")
    assert len(exc.value.problems) == 3


def test_ticket_is_frozen():
    """Layer 1 promises determinism. A mutable ticket cannot promise that."""
    t = Ticket.parse(GOOD)
    with pytest.raises(Exception):
        t.goal = "something else"  # type: ignore[misc]
