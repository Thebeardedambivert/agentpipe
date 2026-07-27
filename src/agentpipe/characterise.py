"""L4: did anything change that nobody asked for?

Validation answers "does it still run". The acceptance checks answer "was this
ticket's work done". Neither asks the third question, and on 27 July 2026 that
question cost a real regression.

funcy #108. agentpipe was asked to make `rcurry` work on methods of built-in
types. The patch passed all 205 of funcy's tests, did not fix the reported bug,
and rewrote

    self_set = {func.__init__.__code__.co_varnames[0]}

into a `getattr(func.__init__, '__name__', None)`, which is the string
'__init__' rather than the name of the first parameter. So `self` stopped being
stripped from every class's argument spec:

    before : names={'a', 'b'},         req_names={'a'}
    after  : names={'a', 'self', 'b'}, req_names={'a', 'self'}

Nothing caught it. Validation could not: the tests pass. The acceptance check
could not: it asks about the bug, not about everything else. The judge could
not: it reads code rather than running it, which is the blind spot Stage 3d
already diagnosed and named.

The idea, and why it needs no cleverness
----------------------------------------

The first design here was going to generate inputs, or record calls and replay
them against the patched code. Both were dropped after one free measurement,
which is the only reason this module is as small as it is.

Instrumenting funcy's own test suite showed it already calls `get_spec` 32
times, 6 of them with a class, including the exact call whose answer the patch
changed. The inputs did not need inventing. They were already being supplied, by
a suite that then reported success.

So: **run the same suite twice, before the patch and after, recording what the
touched functions were called with and what they returned. Then diff.** No
generated inputs, no replay, no pickling, no guessing at what matters. If a
behaviour changed on an input the project already exercises, that shows up as a
difference, and a difference the ticket did not ask for is the finding.

What it does not do, stated plainly
-----------------------------------

- **It sees only what the suite already exercises.** A behaviour no test ever
  triggers is invisible here. This is a floor, not a proof, and calling it a
  proof would be the "no exception is not success" error in a new costume.
- **It requires pytest.** The recorder rides in as a pytest plugin. Every
  repository in the trial uses pytest; the day one does not, this reports that
  it could not run rather than reporting nothing found. Those are different
  facts and are kept different.
- **It cannot tell a wanted change from an unwanted one.** The ticket asked for
  *some* behaviour to change. Deciding which differences are the point and which
  are collateral is a human's job, or a judge's. This module's job is to make
  sure nobody has to notice the difference unaided.

`PYTHONHASHSEED` is pinned for both runs because set and dict reprs otherwise
order differently between processes, which would manufacture differences that
are not real. Found before it could mislead, by running the probe twice.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Memory addresses differ every run and mean nothing. Anything else that differs
# is left alone, deliberately: a normaliser that scrubs too much is a diff that
# reports success because it stopped looking.
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")

# Long reprs are truncated rather than dropped, so a difference is still visible
# even in something that prints a whole data structure.
MAX_REPR = 300

CHARACTERISE_TIMEOUT_SECONDS = 300


class CharacteriseError(Exception):
    """The snapshot could not be taken. Not the same as 'nothing changed'."""


@dataclass(frozen=True)
class Observation:
    """One recorded call: what was asked, and what came back."""

    func: str
    call: str
    result: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.func, self.call)


@dataclass(frozen=True)
class Difference:
    """One call whose answer changed between the two runs."""

    func: str
    call: str
    before: str
    after: str


# Injected onto PYTHONPATH as a pytest plugin rather than written into the
# repository under test. Nothing this project does should leave a file behind in
# somebody else's working tree.
_PLUGIN = '''\
"""Recorder injected by agentpipe.characterise. Writes one JSON line per call."""
import json
import os
import sys

_OUT = os.environ["AGENTPIPE_CHARACTERISE_OUT"]
_TARGETS = [m for m in os.environ["AGENTPIPE_CHARACTERISE_MODULES"].split(",") if m]
_MAX = int(os.environ.get("AGENTPIPE_CHARACTERISE_MAXREPR", "300"))
_records = []


def _clip(text):
    text = str(text)
    return text if len(text) <= _MAX else text[:_MAX] + "...<clipped>"


def _safe_repr(value):
    try:
        return _clip(repr(value))
    except Exception as exc:  # a repr that raises is still information
        return "<unreprable %s: %s>" % (type(value).__name__, exc)


def _wrap(qualname, func):
    def recorder(*args, **kwargs):
        call = ", ".join(
            [_safe_repr(a) for a in args]
            + ["%s=%s" % (k, _safe_repr(v)) for k, v in sorted(kwargs.items())]
        )
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            # An exception is behaviour too, and one of the likelier things a
            # patch changes by accident.
            _records.append({
                "func": qualname,
                "call": _clip(call),
                "result": "raised %s: %s" % (type(exc).__name__, _clip(exc)),
            })
            raise
        _records.append({
            "func": qualname,
            "call": _clip(call),
            "result": _safe_repr(result),
        })
        return result

    recorder.__agentpipe_wrapped__ = func
    try:
        recorder.__name__ = getattr(func, "__name__", qualname)
    except Exception:
        pass
    return recorder


_patched = []
_failed = {}


def pytest_configure(config):
    # The repository under test is not necessarily on sys.path: `pytest` and
    # `python -m pytest` differ on exactly that, and the difference produced a
    # silent empty snapshot the first time this ran for real.
    if config.rootpath and str(config.rootpath) not in sys.path:
        sys.path.insert(0, str(config.rootpath))

    for name in _TARGETS:
        try:
            __import__(name)
        except Exception as exc:
            # Recorded, never swallowed. A module that could not be watched is
            # the difference between "nothing changed" and "nothing was looked
            # at", and those must not arrive at the caller as the same answer.
            _failed[name] = "%s: %s" % (type(exc).__name__, exc)
            continue
        _patched.append(name)
        module = sys.modules[name]
        for attr in dir(module):
            if attr.startswith("_"):
                continue
            original = getattr(module, attr, None)
            if not callable(original) or isinstance(original, type):
                continue
            if getattr(original, "__module__", None) != name:
                continue
            wrapped = _wrap("%s.%s" % (name, attr), original)
            setattr(module, attr, wrapped)
            # A `from x import y` elsewhere copied the binding, so patching the
            # defining module alone would miss the call sites that matter.
            for other in list(sys.modules.values()):
                if other is None or other is module:
                    continue
                try:
                    if getattr(other, attr, None) is original:
                        setattr(other, attr, wrapped)
                except Exception:
                    continue


def pytest_unconfigure(config):
    with open(_OUT, "w", encoding="utf-8") as fh:
        # The meta line first, so the caller can tell an honest empty snapshot
        # from one where nothing was ever instrumented.
        fh.write(json.dumps({"__meta__": {"patched": _patched, "failed": _failed}},
                            sort_keys=True) + "\\n")
        for rec in _records:
            fh.write(json.dumps(rec, sort_keys=True) + "\\n")
'''


def record(
    repo_root: str | Path,
    modules: tuple[str, ...],
    command: str,
    timeout: int = CHARACTERISE_TIMEOUT_SECONDS,
) -> tuple[Observation, ...]:
    """Run the suite once, recording every call into `modules`.

    Raises rather than returning empty when the run could not be instrumented.
    An empty snapshot and a failed snapshot look identical to a caller who only
    checks for differences, and this project has paid for that confusion enough
    times.
    """
    if "pytest" not in command:
        # Saying "cannot run" is a different fact from "found nothing", and the
        # whole value of this module rests on never confusing the two.
        raise CharacteriseError(
            f"the recorder rides in as a pytest plugin, and {command!r} is not "
            f"pytest. This snapshot was not taken; do not read it as 'nothing "
            f"changed'."
        )

    root = Path(repo_root)
    with tempfile.TemporaryDirectory(prefix="agentpipe-char-") as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "agentpipe_characterise_plugin.py").write_text(
            _PLUGIN, encoding="utf-8"
        )
        out = plugin_dir / "observations.jsonl"

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(plugin_dir), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        # Pinned so set and dict reprs order the same way in both runs. Without
        # it the diff invents differences that are only hash randomisation.
        env["PYTHONHASHSEED"] = "0"
        env["AGENTPIPE_CHARACTERISE_OUT"] = str(out)
        env["AGENTPIPE_CHARACTERISE_MODULES"] = ",".join(modules)
        env["AGENTPIPE_CHARACTERISE_MAXREPR"] = str(MAX_REPR)

        full = f"{command} -p agentpipe_characterise_plugin -p no:cacheprovider"
        try:
            proc = subprocess.run(
                full, shell=True, cwd=root, capture_output=True, text=True,
                timeout=timeout, check=False, env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CharacteriseError(f"could not run {full!r}: {exc}") from exc

        if not out.exists():
            raise CharacteriseError(
                f"the recorder never wrote anything, so this is not a snapshot. "
                f"Command was {full!r}, exit {proc.returncode}. "
                f"Last output: {(proc.stdout + proc.stderr).strip()[-400:]!r}"
            )

        observations = []
        meta: dict = {}
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if "__meta__" in rec:
                meta = rec["__meta__"]
                continue
            observations.append(
                Observation(
                    func=rec["func"],
                    call=_ADDRESS.sub("0xADDR", rec["call"]),
                    result=_ADDRESS.sub("0xADDR", rec["result"]),
                )
            )

        # Found on this module's own first live run, which reported "no behaviour
        # changed across 0 recorded calls" because the target could not be
        # imported: `pytest` does not put the repo on sys.path where
        # `python -m pytest` does. A sensor that says all-clear when it was never
        # switched on is worse than no sensor, so both of these raise.
        if not meta.get("patched"):
            raise CharacteriseError(
                f"none of {list(modules)} could be watched, so nothing was "
                f"measured. Reasons: {meta.get('failed') or 'unknown'}. This is "
                f"not a clean snapshot."
            )
        if not observations:
            raise CharacteriseError(
                f"watched {meta['patched']} but recorded no calls at all. The "
                f"suite never exercises them, so a comparison would compare "
                f"nothing and report success."
            )
    return tuple(observations)


def compare(
    before: tuple[Observation, ...], after: tuple[Observation, ...]
) -> tuple[Difference, ...]:
    """Calls the two runs agree on the input for, and disagree on the answer.

    Keyed on (function, arguments), so a call that only happens in one run is not
    a difference: it is a different test path, and reporting it would bury the
    real finding in noise. Only calls present in both runs can disagree.
    """
    first_after: dict[tuple[str, str], str] = {}
    for obs in after:
        first_after.setdefault(obs.key, obs.result)

    seen: set[tuple[str, str]] = set()
    differences: list[Difference] = []
    for obs in before:
        if obs.key in seen or obs.key not in first_after:
            continue
        seen.add(obs.key)
        if first_after[obs.key] != obs.result:
            differences.append(
                Difference(
                    func=obs.func, call=obs.call,
                    before=obs.result, after=first_after[obs.key],
                )
            )
    return tuple(differences)


def report(differences: tuple[Difference, ...], recorded: int) -> str:
    """What changed, or an honest statement of how little was looked at."""
    if not differences:
        return (
            f"characterise: no behaviour changed across {recorded:,} recorded "
            f"calls. This covers only what the suite already exercises."
        )
    lines = [
        f"characterise: {len(differences)} behaviour change(s) across "
        f"{recorded:,} recorded calls. The ticket asked for some change; anything "
        f"here that it did not ask for is collateral:",
        "",
    ]
    for d in differences:
        lines += [
            f"  {d.func}({d.call})",
            f"    before: {d.before}",
            f"    after:  {d.after}",
        ]
    return "\n".join(lines)


def modules_for(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Turn touched file paths into importable module names.

    'funcy/_inspect.py' -> 'funcy._inspect'. Private modules are included on
    purpose: `_inspect` is exactly where the funcy regression lived, and a rule
    that skipped underscored modules would have missed the case this exists for.
    """
    names = []
    for p in paths:
        path = Path(p)
        if path.suffix != ".py":
            continue
        parts = list(path.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    return tuple(dict.fromkeys(names))
