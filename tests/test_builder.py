"""Builder tests.

A fake model, so these are free and deterministic. They test the wiring: that
the right things are called in the right order with the right guards, not that
OpenAI works.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agentpipe.builder import run_builder
from agentpipe.patch import PatchError
from agentpipe.repo import Repo
from agentpipe.telemetry import InMemoryCallStore, MeteredClient, PriceMap
from agentpipe.ticket import Ticket

PRICES = PriceMap({"fake": {"input": 1.0, "cached_input": 0.1, "output": 10.0}})


class FakeOpenAI:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.last_messages = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.last_messages = kwargs["messages"]
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))],
            usage=SimpleNamespace(
                prompt_tokens=1500,
                completion_tokens=120,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "README.md").write_text("# readme\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return Repo(tmp_path)


@pytest.fixture
def ticket():
    return Ticket.parse("""# TASK-1

## Goal
The readme should explain what the project does in its opening paragraph.

## Validation
```
pytest -q
```

## Acceptance
- [ ] readme has an opening paragraph

## Files
- README.md
""")


def client_for(reply: str) -> tuple[MeteredClient, FakeOpenAI]:
    fake = FakeOpenAI(reply)
    return (
        MeteredClient(
            store=InMemoryCallStore(), prices=PRICES,
            client=fake, run_id="run-1",  # type: ignore[arg-type]
        ),
        fake,
    )


GOOD = (
    "--- README.md\n"
    "<<<<<<< SEARCH\n# readme\n=======\n# readme\n\nThis project does a thing.\n"
    ">>>>>>> REPLACE\n"
    "--- end"
)


def test_dry_run_is_the_default(ticket, repo):
    client, _ = client_for(GOOD)
    run_builder(ticket, repo, client, "fake")
    assert repo.read("README.md") == "# readme\n"


def test_apply_writes(ticket, repo):
    client, _ = client_for(GOOD)
    run_builder(ticket, repo, client, "fake", dry_run=False)
    assert "This project does a thing." in repo.read("README.md")


def test_result_carries_the_cost(ticket, repo):
    client, _ = client_for(GOOD)
    r = run_builder(ticket, repo, client, "fake")
    assert r.record.usage.input_tokens == 1500
    assert r.ratio == 12.5
    assert r.cost_usd > 0


def test_pack_hash_is_recorded_against_the_call(ticket, repo):
    client, _ = client_for(GOOD)
    r = run_builder(ticket, repo, client, "fake")
    assert r.record.pack_hash == r.pack_hash


def test_files_outside_the_ticket_are_refused(ticket, repo):
    """The ticket named README.md. The model touched something else."""
    client, _ = client_for(
        "--- notes.md\n<<<<<<< SEARCH\n# notes\n=======\nsneaky\n>>>>>>> REPLACE\n--- end"
    )
    with pytest.raises(PatchError, match="not in the agreed file set"):
        run_builder(ticket, repo, client, "fake", dry_run=False)


def test_prose_reply_raises_after_the_call_is_billed(ticket, repo):
    """The call still happened. The reply was just useless."""
    client, fake = client_for("I'd be happy to help with that!")
    with pytest.raises(PatchError):
        run_builder(ticket, repo, client, "fake")
    assert fake.calls == 1
    assert len(client._store.records) == 1  # type: ignore[attr-defined]


def test_second_identical_run_is_free(ticket, repo):
    """Layer 0's guarantee, reaching all the way up here."""
    client, fake = client_for(GOOD)
    run_builder(ticket, repo, client, "fake")
    r2 = run_builder(ticket, repo, client, "fake")
    assert fake.calls == 1
    assert r2.record.status == "replayed"


def test_feedback_changes_the_attempt_kind(ticket, repo):
    client, _ = client_for(GOOD)
    r = run_builder(ticket, repo, client, "fake", attempt=2, feedback="boom")
    assert r.record.attempt_kind == "validation_retry"
    assert r.record.attempt_index == 2


def test_feedback_makes_it_a_different_call(ticket, repo):
    """Attempt 2 is genuinely new work, so it is genuinely paid for."""
    client, fake = client_for(GOOD)
    run_builder(ticket, repo, client, "fake")
    run_builder(ticket, repo, client, "fake", attempt=2, feedback="boom")
    assert fake.calls == 2


def test_validation_commands_reach_the_model(ticket, repo):
    client, fake = client_for(GOOD)
    run_builder(ticket, repo, client, "fake")
    assert "pytest -q" in fake.last_messages[1]["content"]
