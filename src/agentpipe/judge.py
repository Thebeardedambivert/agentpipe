"""The judge.

Layer 6, the eval gate. Validation proves the code runs. The reviewer finds code
smells. Neither catches the dangerous case: a patch that runs, passes the tests, and
is still wrong or incomplete. The judge reads the produced code and decides whether
it meets the acceptance criteria that no exit code can verify: the semantic ones a
human wrote and only judgment can check ("rejects a negative length with a clear
error", "the message is actionable").

This is Layer 6 Stage 1, and it is only the sensor. It reads and reports; it does
not gate. The gate that stops the run before the expensive review-and-fix stretch is
Stage 2, held back until this is proven to read true, the same one-shot-before-loop
discipline as Layer 2 before Layer 3 and the reviewer before the fixer.

It grades the ticket's check-less acceptance criteria, not a free-form "is this
good" score (a decision made with the user). That keeps it grounded in what the
human asked, off the reviewer's turf, and honest when a ticket gives it nothing to
judge. The verdict is per-criterion and three-state (satisfied / not_satisfied /
uncertain), mirroring checks.py's assess() rather than a numeric score: there is no
threshold to invent, and "uncertain" is the same "not enough to tell is a different
fact from a clear no" lesson that gave assess() its third state.

The output format is the reviewer's, reused: a strict `--- verdict` block of JSON we
parse ourselves, so prose is refused and the provider switch stays a config change.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

from agentpipe.pack import Pack
from agentpipe.repo import Repo, estimate_tokens
from agentpipe.review import _current_files_block
from agentpipe.telemetry import CallRecord, MeteredClient, pack_hash
from agentpipe.ticket import Ticket


class CriterionOutcome(Enum):
    SATISFIED = "satisfied"          # the code clearly meets it
    NOT_SATISFIED = "not_satisfied"  # the code clearly does not
    UNCERTAIN = "uncertain"          # not enough to tell; not a guess


class JudgeVerdict(Enum):
    PASS = "pass"            # every criterion satisfied
    BLOCK = "block"          # at least one not_satisfied or uncertain
    UNGUARDED = "unguarded"  # no semantic criteria to judge; nothing was spent


_OUTCOME_BY_VALUE = {o.value: o for o in CriterionOutcome}


# Stable, never interpolated, cache-friendly, like pack.RULES and REVIEW_RULES.
JUDGE_RULES = """You are a careful reviewer deciding whether a change meets specific acceptance criteria that cannot be checked by running a command.

The code already passes its tests, so whether it runs is not your question. Your question is whether each numbered criterion below is actually met by the code you are shown.

For each criterion, decide one of:
- satisfied: the code clearly meets it.
- not_satisfied: the code clearly does not meet it.
- uncertain: the code does not give you enough to tell. Use this instead of guessing.

Reply with only this block, and nothing else:

--- verdict
[
  {"criterion": 0, "outcome": "satisfied", "reason": "one short sentence"},
  {"criterion": 1, "outcome": "not_satisfied", "reason": "one short sentence"}
]
--- end

Return exactly one entry per criterion, using the criterion's number. outcome is one of: satisfied, not_satisfied, uncertain. No prose, no markdown fences, nothing outside the block."""


def rules_hash() -> str:
    """Identity of the prompt this judge is running.

    Lives next to JUDGE_RULES so it cannot drift from the text it hashes. Layer 6
    Stage 3 records it with every graded criterion, because an accuracy number is
    only comparable to another one produced by the same prompt. Averaging rows from
    before and after a JUDGE_RULES edit would produce a number that describes no
    judge that ever existed, which is this project's favourite kind of lie.
    """
    return hashlib.sha256(JUDGE_RULES.encode("utf-8")).hexdigest()[:16]


_VERDICT_BLOCK = re.compile(
    r"^--- verdict\s*\n(?P<body>.*?)^--- end\s*$",
    re.MULTILINE | re.DOTALL,
)


class JudgeError(Exception):
    """The judge's reply could not be turned into a verdict we trust."""


@dataclass(frozen=True)
class CriterionVerdict:
    """One criterion, the judge's call on it, and why. Validated at construction."""

    criterion: str
    outcome: CriterionOutcome
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, CriterionOutcome):
            raise ValueError(f"outcome must be a CriterionOutcome, got {self.outcome!r}")
        if not self.criterion.strip():
            raise ValueError("criterion verdict has no criterion text")
        if not self.reason.strip():
            raise ValueError("criterion verdict has no reason")


def parse_verdict(reply: str, criteria: tuple[str, ...]) -> tuple[CriterionVerdict, ...]:
    """Pull a verdict out of a judge reply. Strict, like parse_edits/parse_findings.

    A partial judgment is not a judgment, so the reply must cover every criterion
    exactly once. Anything malformed is refused rather than guessed at: prose, a
    non-list body, an unknown outcome, an out-of-range index, a missing reason, or a
    set of indices that does not match the criteria one to one.
    """
    if not reply.strip():
        raise JudgeError("empty reply")

    m = _VERDICT_BLOCK.search(reply)
    if m is None:
        preview = reply.strip()[:200].replace("\n", " ")
        raise JudgeError(
            f"no '--- verdict' block found. The judge replied without the format. "
            f"First 200 chars: {preview!r}"
        )

    try:
        data = json.loads(m.group("body").strip())
    except json.JSONDecodeError as exc:
        raise JudgeError(f"verdict body is not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise JudgeError(f"verdict must be a JSON array, got {type(data).__name__}")

    by_index: dict[int, CriterionVerdict] = {}
    for i, obj in enumerate(data):
        if not isinstance(obj, dict):
            raise JudgeError(f"verdict entry {i} is not an object: {obj!r}")
        missing = {"criterion", "outcome", "reason"} - obj.keys()
        if missing:
            raise JudgeError(
                f"verdict entry {i} is missing field(s): {', '.join(sorted(missing))}"
            )
        idx = obj["criterion"]
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(criteria)):
            raise JudgeError(
                f"verdict entry {i} has criterion index {idx!r}, "
                f"out of range 0..{len(criteria) - 1}"
            )
        if idx in by_index:
            raise JudgeError(f"criterion {idx} judged twice")
        outcome = _OUTCOME_BY_VALUE.get(obj["outcome"])
        if outcome is None:
            raise JudgeError(
                f"verdict entry {i} has unknown outcome {obj['outcome']!r}; "
                f"expected one of {', '.join(_OUTCOME_BY_VALUE)}"
            )
        try:
            by_index[idx] = CriterionVerdict(criteria[idx], outcome, obj["reason"])
        except ValueError as exc:
            raise JudgeError(f"verdict entry {i} is invalid: {exc}") from exc

    if set(by_index) != set(range(len(criteria))):
        judged = sorted(by_index)
        raise JudgeError(
            f"verdict must cover every criterion exactly once: "
            f"judged {judged}, expected 0..{len(criteria) - 1}"
        )

    return tuple(by_index[i] for i in range(len(criteria)))


def build_judge_pack(
    ticket: Ticket, repo: Repo, files: tuple[str, ...], criteria: tuple[str, ...]
) -> Pack:
    """Assemble the judge's context, most-stable-first like pack.build.

    A pure function of its inputs. The criteria are numbered here, and the judge
    references those numbers, so the mapping back is unambiguous.
    """
    system = JUDGE_RULES
    goal = f"# {ticket.ref}\n\n## Goal\n{ticket.goal}"
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria))
    crit_block = f"## Criteria to judge\n{numbered}"
    code = _current_files_block(repo, files)
    user = "\n\n".join([goal, crit_block, "## Code under review", code])

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
class JudgeResult:
    """What the judge decided, and what it cost."""

    verdicts: tuple[CriterionVerdict, ...]
    verdict: JudgeVerdict
    record: Optional[CallRecord]  # None when UNGUARDED: no call was made
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict is JudgeVerdict.PASS

    @property
    def cost_usd(self) -> Decimal:
        return self.record.cost_usd if self.record else Decimal(0)


def run_judge(
    ticket: Ticket,
    repo: Repo,
    client: MeteredClient,
    model: str,
    files: tuple[str, ...],
    attempt_index: int = 0,
    task_ref: Optional[str] = None,
) -> JudgeResult:
    """Judge the produced code against the ticket's check-less acceptance criteria.

    A ticket with no such criteria is UNGUARDED and costs nothing: no model call is
    made. Saying so out loud is the point, the same honesty as the unguarded
    staleness gate. A gate that silently does nothing is the trap this project
    refuses.

    Two parameters exist for Layer 6 Stage 3 and default to today's behaviour, so
    every existing caller is unchanged:

    `attempt_index` is in the idempotency key, so distinct values are distinct paid
    calls rather than cache replays. The eval harness uses it as a *sample* number
    to draw independent judgments of the same case. Note the overload, deliberately,
    because a field whose meaning depends on context is a future bug: everywhere
    else in this codebase attempt_index means "which retry".

    `task_ref` overrides the ticket's own ref in the ledger. The eval harness tags
    its calls `EVAL/<case>` so eval spend is unmistakable in model_calls rather than
    masquerading as a production judgment of the same ticket. It also keeps the two
    from colliding on one idempotency key: judging a case is genuinely different
    work from judging the run that produced it.
    """
    criteria = tuple(c.text for c in ticket.acceptance if not c.check)
    if not criteria:
        return JudgeResult(
            verdicts=(), verdict=JudgeVerdict.UNGUARDED, record=None,
            reason=(
                "no acceptance criteria a judge can grade (every criterion is "
                "machine-checked, or the ticket has none); unguarded, nothing spent"
            ),
        )

    pack = build_judge_pack(ticket, repo, files, criteria)
    record = client.call(
        messages=pack.as_list(),
        model=model,
        role="judge",
        attempt_kind="eval",
        attempt_index=attempt_index,
        task_ref=task_ref or ticket.ref,
    )
    verdicts = parse_verdict(record.content, criteria)
    verdict = (
        JudgeVerdict.PASS
        if all(v.outcome is CriterionOutcome.SATISFIED for v in verdicts)
        else JudgeVerdict.BLOCK
    )
    return JudgeResult(verdicts=verdicts, verdict=verdict, record=record)


# Worst first: a problem should be the first thing a human reads.
_REPORT_ORDER = {
    CriterionOutcome.NOT_SATISFIED: 0,
    CriterionOutcome.UNCERTAIN: 1,
    CriterionOutcome.SATISFIED: 2,
}


def report_judge(result: JudgeResult) -> str:
    """The judgment in one block a human can read, worst criterion first."""
    headline = {
        JudgeVerdict.PASS: "PASS (every criterion satisfied)",
        JudgeVerdict.BLOCK: "BLOCK (a criterion is not satisfied or uncertain)",
        JudgeVerdict.UNGUARDED: "UNGUARDED (no criteria a judge can grade)",
    }[result.verdict]

    lines = [f"judge      {headline}"]
    if result.record is not None:
        u = result.record.usage
        lines += [
            f"cost       ${result.cost_usd}",
            f"actual     in={u.input_tokens:,} out={u.output_tokens:,}",
        ]

    if result.verdict is JudgeVerdict.UNGUARDED:
        lines += ["", result.reason]
    else:
        lines.append("")
        for v in sorted(result.verdicts, key=lambda v: _REPORT_ORDER[v.outcome]):
            lines.append(f"  [{v.outcome.value:<14}] {v.criterion}")
            lines.append(f"                   {v.reason}")

    lines += ["", "advisory only. Nothing was gated; the gate arrives in Stage 2."]
    return "\n".join(lines)
