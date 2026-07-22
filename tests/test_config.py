"""ModelMap tests.

The one thing that matters: it DEFAULTS instead of refusing, unlike PriceMap. A
missing map means every role uses the base model and nothing breaks. A present map
overrides only the roles it names.
"""

from __future__ import annotations

import json

import pytest

from agentpipe.config import ModelMap


def test_unset_defaults_every_role_to_the_base(monkeypatch):
    monkeypatch.delenv("AGENTPIPE_MODELS", raising=False)
    m = ModelMap.from_env(base="base-m")
    assert m.for_role("reviewer") == "base-m"
    assert m.for_role("fixer") == "base-m"
    assert m.for_role("anything") == "base-m"


def test_a_file_overrides_named_roles_only(tmp_path, monkeypatch):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"fixer": "cheap-m"}), encoding="utf-8")
    monkeypatch.delenv("AGENTPIPE_MODELS", raising=False)
    m = ModelMap.from_env(base="base-m", path=str(p))
    assert m.for_role("fixer") == "cheap-m"       # named -> overridden
    assert m.for_role("reviewer") == "base-m"     # unnamed -> base


def test_env_var_is_read_when_no_path_given(tmp_path, monkeypatch):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"reviewer": "rev-m"}), encoding="utf-8")
    monkeypatch.setenv("AGENTPIPE_MODELS", str(p))
    m = ModelMap.from_env(base="base-m")
    assert m.for_role("reviewer") == "rev-m"


def test_a_base_is_required():
    with pytest.raises(ValueError, match="base model"):
        ModelMap(base="")


def test_a_non_object_file_is_refused(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(ValueError, match="role -> model"):
        ModelMap.from_env(base="base-m", path=str(p))
