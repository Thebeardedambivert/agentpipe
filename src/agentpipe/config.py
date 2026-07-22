"""Model routing: which model each role uses.

This is the I4 cost lever. The builder writes whole features and may want the
capable model; the fixer makes a one-line repair and does not. Routing the narrow
work to a cheaper model is, per PLAN.md, the biggest single saving in the system,
and it runs many times in a loop, so the saving compounds.

`ModelMap` mirrors `PriceMap`, with one deliberate difference: it DEFAULTS instead
of refusing. `PriceMap.from_env` raises when unset, because a missing price map is
dangerous: a wrong cost is worse than no cost, it looks right. A missing model map
is harmless: every role simply uses the base model and the pipeline runs exactly as
it did before routing existed. So this is the one config allowed to default, and it
does. Set `AGENTPIPE_MODELS` (or pass a path) to a JSON file to override per role:

    { "reviewer": "gpt-5.4-mini", "fixer": "gpt-5.4-nano" }

A role not named in the file falls back to the base model. `models.json` is
gitignored like `prices.json`.
"""

from __future__ import annotations

import json
import os
from typing import Mapping


class ModelMap:
    def __init__(self, base: str, overrides: Mapping[str, str] | None = None) -> None:
        if not base:
            # The base is the floor every role falls back to, so it must exist.
            # Everything else is optional; this is not.
            raise ValueError("ModelMap needs a base model")
        self._base = base
        self._overrides = dict(overrides or {})

    @classmethod
    def from_env(cls, base: str, path: str | None = None) -> "ModelMap":
        """Load overrides from a JSON file, or default every role to `base`.

        Unset is not an error here, unlike PriceMap: no file means no overrides,
        which means the pipeline behaves exactly as it did before routing. That is
        a safe default, so it is allowed to be the default.
        """
        path = path or os.environ.get("AGENTPIPE_MODELS")
        if not path:
            return cls(base)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(
                f"AGENTPIPE_MODELS must be a JSON object of role -> model, "
                f"got {type(data).__name__}"
            )
        return cls(base, data)

    def for_role(self, role: str) -> str:
        """The model this role runs on, or the base model if the role is unset."""
        return self._overrides.get(role, self._base)
