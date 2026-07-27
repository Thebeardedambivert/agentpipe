"""Test-session setup that applies to every test file.

One job: stop the test suite leaving background git daemons behind.

Most fixtures here build a real git repository in a temp directory, because this
project refuses test doubles that can drift from the real thing (see
tests/test_store_contract.py for what that lesson cost). That decision is right
and stays. The cost of it was not noticed until a dev machine fell over.

On Git for Windows, `git add` spawns:

    git fsmonitor--daemon run --detach --ipc-threads=8

Detached, long-lived by design, and it carries on watching a directory pytest is
about to delete. With 18 git call sites across 8 test files, a full run leaves a
crowd of them behind, each holding eight IPC threads open on a path that no longer
exists.

The fix is config, not code: git reads GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n /
GIT_CONFIG_VALUE_n from the environment and applies them to every invocation.
Subprocesses inherit the environment, so setting it once here covers all 18 call
sites and any added later, without touching a single fixture.

Measured before writing, because the first attempt at this got it wrong by
hardening `git init` only:

    plain init + plain add        -> +1 daemon
    hardened init + plain add     -> +1 daemon
    hardened init + hardened add  -> +0 daemons

Set at import rather than in a fixture so it is in place before collection, and
therefore before any module-scoped fixture can run a git command.
"""

from __future__ import annotations

import os

# Applied to every git process this test session spawns.
_GIT_CONFIG = {
    "core.fsmonitor": "false",       # no detached watcher daemon
    "gc.auto": "0",                  # no background garbage collection
    "maintenance.auto": "false",     # no background maintenance
    "protocol.version": "2",         # nothing here talks to a remote anyway
}

os.environ["GIT_CONFIG_COUNT"] = str(len(_GIT_CONFIG))
for _i, (_key, _value) in enumerate(_GIT_CONFIG.items()):
    os.environ[f"GIT_CONFIG_KEY_{_i}"] = _key
    os.environ[f"GIT_CONFIG_VALUE_{_i}"] = _value
