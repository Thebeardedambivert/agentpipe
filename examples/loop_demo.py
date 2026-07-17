"""A free, no-API-key demo of the Layer 3 loop.

It builds a throwaway git repo and a ticket whose validation passes only when
answer.txt contains exactly '42'. A FAKE model answers wrong first ('7'), then
right ('42'), so you can watch the loop fail, feed the failure back, and fix
itself, without spending a cent or needing an OpenAI key.

Run it from the repo root:
    .venv/Scripts/python.exe examples/loop_demo.py

Try changing REPLIES to [WRONG, WRONG, WRONG] and watch it end in GAVE UP.
"""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agentpipe.loop import report_loop, run_loop
from agentpipe.repo import Repo
from agentpipe.telemetry import InMemoryCallStore, MeteredClient, PriceMap
from agentpipe.ticket import Ticket


class FakeModel:
    """Returns canned replies in order, so no real model is ever called."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = 0

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=reply), finish_reason="stop")],
            usage=SimpleNamespace(
                prompt_tokens=1500, completion_tokens=120,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        )


WRONG = "--- answer.txt\n7\n--- end"
RIGHT = "--- answer.txt\n42\n--- end"
REPLIES = [WRONG, RIGHT]  # try [WRONG, WRONG, WRONG] to see it give up

VALIDATION = (
    'python -c "import os, sys; sys.exit(0 if os.path.exists(\'answer.txt\') '
    "and open('answer.txt').read().strip() == '42' else 1)\""
)

ticket = Ticket.parse(f"""# DEMO

## Goal
answer.txt should contain the number 42 and nothing else.

## Validation
```
{VALIDATION}
```

## Acceptance
- [ ] answer.txt contains 42

## Files
- answer.txt
""")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    repo = Repo(root)

    fake = FakeModel(REPLIES)
    client = MeteredClient(
        store=InMemoryCallStore(),
        prices=PriceMap({"fake": {"input": 1.0, "cached_input": 0.1, "output": 10.0}}),
        client=fake, run_id="demo",
    )

    print("Running the loop. Fake model answers '7' first, then '42'.\n")
    result = run_loop(ticket, repo, client, "fake", max_attempts=3)
    print(report_loop(result))
    print(f"\nFinal answer.txt on disk: {(root / 'answer.txt').read_text().strip()!r}")
    print(f"Model was called {fake.calls} time(s).")
