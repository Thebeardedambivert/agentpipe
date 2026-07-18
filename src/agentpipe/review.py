"""The reviewer.

A second opinion on code that already passes its tests. Validation proves the
code *runs*; it does not prove the code is *good*. The reviewer reads the passing
change and returns ranked, structured findings: correctness gaps, missing edge
cases, unclear code, conventions ignored. Things a test suite the model did not
write cannot catch.

This is Layer 5 Stage 1, and it is deliberately only the sensor. It reads; it
does not write. Findings are advisory: printed for a human, acted on by nobody.
The fixer that repairs against them is Stage 2, and it stays unbuilt until this
one is proven to read true, for the same reason Layer 2 (build, one shot) came
before Layer 3 (the loop): a loop hides bugs, so prove the one-shot first.

Two ideas from the rest of the project meet here again:

1. Refuse ambiguity at the boundary. parse_findings is as unforgiving as
   patch.py's parse_edits. A reviewer that replies with prose, or half-formed
   JSON, is refused, not guessed at. A sensor whose output cannot be read
   reliably is not a sensor.
2. Make the invalid state unrepresentable. A Finding with an unknown severity or
   an empty issue cannot be constructed, so it cannot be stored, ranked, or
   reported. Same trick as CallRecord.__post_init__.

The findings format is a JSON array inside a `--- findings` / `--- end` block:
the same delimiter shape patch.py already uses, so prose around the JSON is
refused exactly the way parse_edits refuses prose, and the JSON is parsed and
validated by us rather than by a provider's structured-output feature. That keeps
the OpenRouter/GLM switch a config change, not a code change (see STATE.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from agentpipe.pack import Pack, _ticket_block
from agentpipe.repo import Repo, estimate_tokens
from agentpipe.telemetry import CallRecord, MeteredClient, pack_hash
from agentpipe.ticket import Ticket

# Worst first. One source of truth for both validation (is this a real severity?)
# and ranking (which finding matters more?), so the two can never disagree about
# what the levels are.
SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


# Stable, never interpolated, cache-friendly. Every character here is one the
# cached-input discount can reuse forever, so nothing ticket-specific or
# time-specific belongs in it. Same discipline as pack.RULES.
REVIEW_RULES = """You are a careful senior engineer reviewing a change in an existing repository.

The change already passes its validation commands, so it runs. Checking that it runs is not your job. Your job is to find what is wrong that the tests do not catch: correctness gaps, unhandled edge cases, unsafe assumptions, unclear code, and deviations from the conventions visible in the surrounding files.

Rules:
- Only report problems in the files you are shown.
- Report what is wrong, not what the code does. Do not restate the code.
- If the code is fine, return an empty list. An empty review is a valid review. Do not invent problems to fill it.
- Give each finding an honest severity. Do not order them yourself.

Reply with only this block, and nothing outside it:

--- findings
[
  {"severity": "high", "file": "path/to/file.py", "line": 42, "issue": "one sentence on what is wrong"}
]
--- end

severity is one of: critical, high, medium, low.
line is the line the problem is on, or null when it is not about a single line.
Return [] between the markers when there is nothing worth reporting.
No prose, no markdown fences, nothing before or after the block."""


# Same anchored, non-greedy idea as patch.py's _BLOCK: the body cannot terminate
# itself early on a stray "--- end" that is not at the start of a line.
_FINDINGS_BLOCK = re.compile(
    r"^--- findings\s*\n(?P<body>.*?)^--- end\s*$",
    re.MULTILINE | re.DOTALL,
)


class ReviewError(Exception):
    """The reviewer's reply could not be turned into findings we trust."""


@dataclass(frozen=True)
class Finding:
    """One thing the reviewer thinks is wrong, and how much it matters.

    Frozen and validated at construction, so an invalid finding cannot exist.
    `line` is optional on purpose: a finding like "this file has no error
    handling" is about the whole file, and forcing a line number would make the
    reviewer invent one, which is a lie dressed as precision.
    """

    severity: str
    file: str
    issue: str
    line: Optional[int] = None

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(
                f"unknown severity {self.severity!r}; "
                f"expected one of {', '.join(SEVERITIES)}"
            )
        if not self.file.strip():
            raise ValueError("finding has no file")
        if not self.issue.strip():
            raise ValueError("finding has no issue text")
        # bool is an int subclass, so exclude it explicitly: True is not a line.
        if self.line is not None and (
            isinstance(self.line, bool) or not isinstance(self.line, int) or self.line < 1
        ):
            raise ValueError(f"line must be a positive integer or null, got {self.line!r}")

    @property
    def rank(self) -> int:
        """Lower is worse. critical=0, low=3. For sorting worst-first."""
        return _SEVERITY_RANK[self.severity]


def parse_findings(reply: str) -> tuple[Finding, ...]:
    """Pull findings out of a reviewer reply. Strict, like parse_edits.

    We do not strip markdown fences, tolerate a body that is not a JSON list, or
    accept a finding missing a field. If the reviewer did not follow the format,
    the right response is to refuse and (in Stage 2) retry, not to reconstruct
    what it probably meant. An empty array between the markers is not an error:
    it is the reviewer saying, validly, that the code is fine.
    """
    if not reply.strip():
        raise ReviewError("empty reply")

    m = _FINDINGS_BLOCK.search(reply)
    if m is None:
        preview = reply.strip()[:200].replace("\n", " ")
        raise ReviewError(
            f"no '--- findings' block found. The reviewer replied without the "
            f"format. First 200 chars: {preview!r}"
        )

    body = m.group("body").strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"findings body is not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise ReviewError(
            f"findings must be a JSON array, got {type(data).__name__}"
        )

    findings: list[Finding] = []
    for i, obj in enumerate(data):
        if not isinstance(obj, dict):
            raise ReviewError(f"finding {i} is not an object: {obj!r}")
        missing = {"severity", "file", "issue"} - obj.keys()
        if missing:
            raise ReviewError(
                f"finding {i} is missing required field(s): "
                f"{', '.join(sorted(missing))}"
            )
        try:
            findings.append(
                Finding(
                    severity=obj["severity"],
                    file=obj["file"],
                    issue=obj["issue"],
                    line=obj.get("line"),
                )
            )
        except ValueError as exc:
            # Turn the construction-time guard into the same ReviewError the rest
            # of this function raises, so a caller has one exception to catch.
            raise ReviewError(f"finding {i} is invalid: {exc}") from exc

    return tuple(findings)


def _current_files_block(repo: Repo, files: tuple[str, ...]) -> str:
    """The files under review, as they are on disk right now.

    Same `--- path` / `--- end` framing pack.py uses for the builder, so the
    reviewer sees the code the way the builder wrote it.
    """
    if not files:
        return "(no files under review)"
    chunks = [f"--- {path}\n{repo.read(path)}\n--- end" for path in files]
    return "\n\n".join(chunks)


def build_review_pack(ticket: Ticket, repo: Repo, files: tuple[str, ...]) -> Pack:
    """Assemble the reviewer's context, most-stable-first like pack.build.

    A pure function of its inputs, for the same reason pack.build is: a
    deterministic pack is what makes the hash, the idempotency, and the cache
    discount real.

    It reuses pack._ticket_block on purpose. The reviewer must read the ticket
    the same way the builder did, or the two quietly disagree about what was
    asked, and two specs of "what done means" that disagree is the exact bug this
    project keeps paying for.
    """
    system = REVIEW_RULES
    asked = _ticket_block(ticket)                      # what was asked
    produced = _current_files_block(repo, files)       # what was produced
    user = "\n\n".join([asked, "## Code under review", produced])

    messages = (
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    )
    return Pack(
        messages=messages,
        hash=pack_hash(list(messages)),
        tokens=estimate_tokens(system + user),
    )


@dataclass(frozen=True)
class ReviewResult:
    """What the reviewer said, and what it cost, for a human and for a test."""

    ticket_ref: str
    pack_hash: str
    pack_tokens: int
    findings: tuple[Finding, ...]  # ranked worst-first, filtered by min_severity
    record: CallRecord

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def cost_usd(self):
        return self.record.cost_usd


def run_review(
    ticket: Ticket,
    repo: Repo,
    client: MeteredClient,
    model: str,
    files: tuple[str, ...],
    min_severity: str = "low",
    max_output_tokens: Optional[int] = None,
) -> ReviewResult:
    """One review call. No loop, no fixer, nothing written.

    Goes through the one door (client.call, role="reviewer"), so the review's
    cost lands in model_calls for free and Stage 3's audit has a row to join to.

    `min_severity` is a hypothesis, not a tuned constant: it says how low a
    severity is still worth surfacing. Default "low" keeps everything, because
    this stage has no data yet on where the useful/nitpick line sits. What would
    settle it: Stage 3's review_findings table, which records which severities
    ever lead to a real fix.

    `max_output_tokens` is left unset by default. The builder sets an output
    budget to avoid a reasoning model spending its whole small allowance on
    thinking and returning nothing; an unset budget cannot cause that (it only
    risks a larger, costlier reply). If real runs show the reviewer truncated
    (finish_reason='length') or empty, set one here, derived the way
    builder.output_budget is, not guessed.
    """
    if min_severity not in _SEVERITY_RANK:
        raise ValueError(
            f"unknown min_severity {min_severity!r}; "
            f"expected one of {', '.join(SEVERITIES)}"
        )

    pack = build_review_pack(ticket, repo, files)

    kwargs: dict[str, Any] = {}
    if max_output_tokens is not None:
        kwargs["max_completion_tokens"] = max_output_tokens

    record = client.call(
        messages=pack.as_list(),
        model=model,
        role="reviewer",
        attempt_kind="review",
        attempt_index=0,
        task_ref=ticket.ref,
        **kwargs,
    )

    # An empty reviewer reply is recorded by the seam as status='error' with
    # content="", so parse_findings raises here rather than silently producing an
    # empty (falsely clean) review. That surfaces the failure instead of hiding it.
    findings = parse_findings(record.content)

    threshold = _SEVERITY_RANK[min_severity]
    kept = tuple(
        sorted(
            (f for f in findings if f.rank <= threshold),
            key=lambda f: (f.rank, f.file, f.line or 0),
        )
    )
    return ReviewResult(ticket.ref, pack.hash, pack.tokens, kept, record)


def report_review(result: ReviewResult) -> str:
    """The review in one block a human can read, worst finding first."""
    u = result.record.usage
    lines = [
        f"review     {result.ticket_ref}",
        f"pack       {result.pack_tokens:,} tokens est, hash {result.pack_hash}",
        f"actual     in={u.input_tokens:,} (cached {u.cached_input_tokens:,}) "
        f"out={u.output_tokens:,}",
        f"cost       ${result.cost_usd}",
        "",
    ]
    if result.clean:
        lines.append("clean. The reviewer found nothing worth reporting.")
    else:
        lines.append(f"{len(result.findings)} finding(s), worst first:")
        for f in result.findings:
            where = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"  [{f.severity:<8}] {where}")
            lines.append(f"             {f.issue}")

    lines += ["", "advisory only. Nothing was changed; the fixer arrives in Stage 2."]
    return "\n".join(lines)
