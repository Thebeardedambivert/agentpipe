"""The eval dataset: who judges the judge.

    python -m agentpipe.evals --dry-run     # validate the dataset, free
    python -m agentpipe.evals               # grade the judge, costs real money

Layer 6 Stage 3. Stage 1 built the judge. Stage 2 gave it command authority: with
--gate, a BLOCK sends the builder back to work and spends another attempt. That
was done knowingly, and the caveat was written down in plans/layer6.md rather than
glossed over: we handed an unmeasured sensor the wheel. This module is what
removes the caveat.

Two failures are live and neither is currently observable:

    false pass    labelled not_satisfied, judged satisfied
                  the gate waves wrong code through and the layer was theatre
    false block   labelled satisfied, judged not_satisfied
                  the gate burns a rebuild attempt on correct code, every time

Before Stage 2 only the first mattered, and it was advisory. After Stage 2 both
cost money on every run.

A case is a directory, not a bespoke file format, and that is the load-bearing
decision here. It holds a real ticket parsed by Ticket.from_file, real code files
read through the real Repo, and a labels file. Anything else would mean a second
way to read a ticket, and a second implementation that can drift from the first is
the InMemoryCallStore / PostgresCallStore bug in a new costume. The eval must feed
the judge exactly what production feeds it, or it is grading a judge that does not
exist.

What this module refuses to do:

- It reports counts, never percentages. A rate over eight cases invites exactly
  the over-reading CLAUDE.md forbids: one flipped verdict moves an eight-case rate
  by twelve points and still reads as a measurement. `graded` is printed next to
  every count, so anyone can divide while looking at what they are dividing.
- It sets no threshold and grades nothing pass/fail. There is no number here that
  says the judge is good enough. That is a human decision made from the counts.
- It never merges real and constructed cases into one figure. The constructed half
  was written by the same person who wrote the judge's prompt, and a number that
  hides that is flattering itself.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from agentpipe.config import ModelMap
from agentpipe.evalstore import PROVENANCE
from agentpipe.judge import (
    CriterionOutcome,
    JudgeError,
    JudgeResult,
    JudgeVerdict,
    build_judge_pack,
    rules_hash,
    run_judge,
)
from agentpipe.repo import Repo
from agentpipe.telemetry import MeteredClient
from agentpipe.ticket import Ticket, TicketError

# What a human may label. The judge has three states; a label has two. A labeller
# who is uncertain has not finished making the case, so 'uncertain' is an answer
# the judge may give and never a ground truth it can be measured against.
LABELLABLE = (CriterionOutcome.SATISFIED, CriterionOutcome.NOT_SATISFIED)

# Eval calls land in model_calls under this prefix. They are real billed judge
# calls and they will show up in ratio_by_role under role='judge', which is
# correct: hiding real spend is the quiet-cost trap this project exists to refuse.
# The prefix is what makes them filterable when someone wants a production-only cut.
TASK_REF_PREFIX = "EVAL/"


class EvalError(Exception):
    """A case that cannot be trusted to measure anything.

    Raised at load time, before any model call, for the same reason TicketError is:
    a malformed case discovered after the spend has produced a confident wrong
    number, which is worse than no number.
    """


@dataclass(frozen=True)
class Label:
    """What a human says is true of this code, for one criterion, and why."""

    criterion: int
    text: str
    expect: CriterionOutcome
    why: str

    def __post_init__(self) -> None:
        if self.expect not in LABELLABLE:
            raise EvalError(
                f"criterion {self.criterion} is labelled {self.expect.value!r}. "
                f"A label must be one of {', '.join(o.value for o in LABELLABLE)}. "
                f"If the ground truth is genuinely unclear, the case is not ready "
                f"to be a case; 'uncertain' is the judge's answer, not yours."
            )
        if not self.text.strip():
            raise EvalError(f"label {self.criterion} has no criterion text")
        if not self.why.strip():
            # The 'why' is what you re-read when the judge disagrees, to work out
            # whether the judge is wrong or the label is. Without it a
            # disagreement is unresolvable and the dataset rots.
            raise EvalError(
                f"label {self.criterion} has no 'why'. When the judge disagrees "
                f"with this label, the why is what tells you which of the two is "
                f"wrong."
            )
        if self.criterion < 0:
            raise EvalError(f"negative criterion index: {self.criterion}")


@dataclass(frozen=True)
class EvalCase:
    """A ticket, the code that was produced for it, and the truth about that code.

    Validated at construction, the CallRecord.__post_init__ lesson applied to the
    dataset: an invalid case you can build is an invalid case you will build, and
    a dataset that lies produces a confident accuracy number that nothing will ever
    contradict.
    """

    name: str
    ticket: Ticket
    code_dir: Path
    files: tuple[str, ...]
    labels: tuple[Label, ...]
    provenance: str
    source: Optional[str] = None
    note: str = ""

    @property
    def criteria(self) -> tuple[str, ...]:
        """The check-less acceptance criteria, exactly as run_judge selects them.

        Derived through the same expression the judge uses rather than stored, so
        the two cannot disagree about what is judgeable.
        """
        return tuple(c.text for c in self.ticket.acceptance if not c.check)

    @property
    def expected_verdict(self) -> JudgeVerdict:
        """What the gate should have decided, derived from the labels.

        PASS only when every criterion is labelled satisfied, mirroring run_judge's
        own rule. Derived, never stored: a stored verdict could disagree with the
        labels it summarises, and then which one is the truth?
        """
        return (
            JudgeVerdict.PASS
            if all(lb.expect is CriterionOutcome.SATISFIED for lb in self.labels)
            else JudgeVerdict.BLOCK
        )

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE:
            raise EvalError(
                f"case {self.name}: unknown provenance {self.provenance!r}; "
                f"expected one of {', '.join(PROVENANCE)}"
            )
        if self.provenance == "real" and not (self.source or "").strip():
            raise EvalError(
                f"case {self.name}: provenance is 'real' but no source is named. "
                f"A real case must say which run it came from, or nobody can check "
                f"that it is real."
            )
        if not self.files:
            raise EvalError(f"case {self.name}: no files for the judge to read")

        criteria = self.criteria
        if not criteria:
            # An unjudgeable case in a judge eval is a mistake, not a data point:
            # run_judge would return UNGUARDED and spend nothing, so the case would
            # silently contribute zero rows while looking like it participated.
            raise EvalError(
                f"case {self.name}: the ticket has no check-less acceptance "
                f"criteria, so there is nothing for the judge to grade. Every "
                f"criterion carries a `check:`, which makes this a checks.py case, "
                f"not a judge case."
            )

        indices = [lb.criterion for lb in self.labels]
        if sorted(indices) != list(range(len(criteria))):
            # The same completeness rule parse_verdict enforces on the judge. A
            # partially labelled case is not a case: the unlabelled criteria would
            # be graded against nothing and silently dropped from the counts.
            raise EvalError(
                f"case {self.name}: labels must cover every criterion exactly "
                f"once. Labelled {sorted(indices)}, expected "
                f"0..{len(criteria) - 1}."
            )

        for lb in self.labels:
            if lb.text.strip() != criteria[lb.criterion].strip():
                # The drift guard, and the reason labels carry the text at all.
                # Without this, reordering the ticket's acceptance bullets silently
                # repoints every label and the eval reports a confident wrong
                # number that nothing downstream can detect. Same bug shape as the
                # five in STATE.md: nothing errors, everything lies.
                raise EvalError(
                    f"case {self.name}: label {lb.criterion} says\n"
                    f"    {lb.text!r}\n"
                    f"but the ticket's criterion {lb.criterion} says\n"
                    f"    {criteria[lb.criterion]!r}\n"
                    f"The ticket moved and the labels did not. Fix case.json, or "
                    f"you will be grading the judge against the wrong criteria."
                )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_case(directory: str | Path) -> EvalCase:
    """Read one case directory into a validated EvalCase.

    Everything that can be wrong with a case is wrong here, before a model is
    called. That is the ticket contract's discipline applied one level up: the
    cheapest possible failure, and a free one.
    """
    d = Path(directory)
    name = d.name

    meta_path = d / "case.json"
    if not meta_path.exists():
        raise EvalError(f"case {name}: no case.json in {d}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalError(f"case {name}: case.json is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise EvalError(f"case {name}: case.json must be an object")

    ticket_path = d / "ticket.md"
    if not ticket_path.exists():
        raise EvalError(f"case {name}: no ticket.md in {d}")
    try:
        ticket = Ticket.from_file(ticket_path)
    except TicketError as exc:
        # A case whose ticket the pipeline would refuse is not measuring the
        # pipeline. Surfacing it as an EvalError keeps the failure in one place.
        raise EvalError(f"case {name}: its ticket is not valid:\n{exc}") from exc

    code_dir = d / "code"
    if not code_dir.is_dir():
        raise EvalError(f"case {name}: no code/ directory in {d}")

    files = tuple(meta.get("files") or ())
    for f in files:
        if not (code_dir / f).is_file():
            raise EvalError(
                f"case {name}: case.json names file {f!r}, which is not in code/"
            )

    raw_labels = meta.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise EvalError(f"case {name}: case.json has no 'labels' array")

    labels: list[Label] = []
    for i, obj in enumerate(raw_labels):
        if not isinstance(obj, dict):
            raise EvalError(f"case {name}: label {i} is not an object")
        missing = {"criterion", "text", "expect", "why"} - obj.keys()
        if missing:
            raise EvalError(
                f"case {name}: label {i} is missing field(s): "
                f"{', '.join(sorted(missing))}"
            )
        expect = {o.value: o for o in CriterionOutcome}.get(obj["expect"])
        if expect is None:
            raise EvalError(
                f"case {name}: label {i} has unknown expect {obj['expect']!r}"
            )
        if not isinstance(obj["criterion"], int) or isinstance(obj["criterion"], bool):
            raise EvalError(
                f"case {name}: label {i} criterion must be an integer index"
            )
        labels.append(Label(obj["criterion"], obj["text"], expect, obj["why"]))

    return EvalCase(
        name=name,
        ticket=ticket,
        code_dir=code_dir,
        files=files,
        labels=tuple(sorted(labels, key=lambda lb: lb.criterion)),
        provenance=meta.get("provenance", ""),
        source=meta.get("source"),
        note=meta.get("note", ""),
    )


def load_cases(root: str | Path, only: Optional[str] = None) -> tuple[EvalCase, ...]:
    """Every case under root, sorted by name. Sorted so a report is diffable."""
    r = Path(root)
    if not r.is_dir():
        raise EvalError(f"no case directory at {r}")
    dirs = sorted(d for d in r.iterdir() if d.is_dir() and (d / "case.json").exists())
    if only:
        dirs = [d for d in dirs if d.name == only]
        if not dirs:
            raise EvalError(f"no case named {only!r} under {r}")
    if not dirs:
        raise EvalError(f"no cases found under {r}")
    return tuple(load_case(d) for d in dirs)


# These repos live for milliseconds inside a temp directory and are then deleted.
# Nothing that outlives them, or runs in the background, has any business starting
# up for them.
#
# On Git for Windows a bare `git add` spawns `git fsmonitor--daemon run --detach
# --ipc-threads=8`. Detached, by design long-lived, and it goes on watching a
# directory we are about to delete. At forty git calls per command, repeated over
# a session, that is a lot of orphaned daemons on a machine that then fell over.
#
# Measured, not assumed, because the first version of this got it wrong:
#
#   plain init + plain add          -> +1 daemon
#   hardened init + PLAIN add       -> +1 daemon   (hardening init alone does nothing)
#   hardened init + hardened add    -> +0 daemons
#
# So the flags go on EVERY invocation. `-c` before the subcommand, not after.
_GIT = [
    "git",
    "-c", "core.fsmonitor=false",     # no detached watcher daemon
    "-c", "gc.auto=0",                # no background garbage collection
    "-c", "maintenance.auto=false",   # no background maintenance
]


def materialise(case: EvalCase, dest: str | Path) -> Repo:
    """Lay a case's code out as a real git repository the judge can read.

    A real Repo, not a double. The judge reads through repo.read() in production,
    and a stand-in that behaves almost the same is how this project once had
    fifteen passing tests over a broken store. `git init` costs about 50ms and buys
    the guarantee that the object under test is the object that ships.
    """
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.code_dir, d, dirs_exist_ok=True)
    subprocess.run(_GIT + ["init", "-q"], cwd=d, check=True,
                   capture_output=True, text=True)
    subprocess.run(_GIT + ["add", "-A"], cwd=d, check=True,
                   capture_output=True, text=True)
    return Repo(d)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CriterionScore:
    """One criterion, what was labelled, what the judge said.

    The named failures below are derived from the raw (expected, actual) pair
    rather than stored as a category. The matrix is the primitive; these are names
    for its cells. Storing a category would let it disagree with the pair it
    describes, and then the report and the table would be two sources of truth.
    """

    case_name: str
    provenance: str
    criterion_index: int
    criterion: str
    expected: CriterionOutcome
    actual: CriterionOutcome
    sample: int
    model: str
    source: Optional[str] = None
    call_key: Optional[str] = None
    label_why: str = ""
    judge_reason: str = ""

    @property
    def agrees(self) -> bool:
        return self.actual is self.expected

    @property
    def is_false_pass(self) -> bool:
        """The gate would wave wrong code through. The dangerous one."""
        return (
            self.expected is CriterionOutcome.NOT_SATISFIED
            and self.actual is CriterionOutcome.SATISFIED
        )

    @property
    def is_false_block(self) -> bool:
        """The gate would burn a rebuild attempt on correct code."""
        return (
            self.expected is CriterionOutcome.SATISFIED
            and self.actual is CriterionOutcome.NOT_SATISFIED
        )

    @property
    def is_abstain(self) -> bool:
        """The judge would not say. Blocks either way, but for an honest reason.

        Reported split by label, never as one bucket: abstaining on a wrong patch
        reaches the right gate decision by a weak route, and abstaining on a correct
        one is a false block that at least admits doubt. Same fact, different costs.
        """
        return self.actual is CriterionOutcome.UNCERTAIN


@dataclass(frozen=True)
class CaseScore:
    """One case, one sample: the gate decision, and whether it was reached honestly."""

    case_name: str
    provenance: str
    sample: int
    expected_verdict: JudgeVerdict
    actual_verdict: Optional[JudgeVerdict]  # None when the judge's reply was unusable
    criteria: tuple[CriterionScore, ...]
    cost_usd: Decimal
    error: Optional[str] = None
    # True when the seam answered from cache instead of calling the model. The
    # sample is real (it is the answer that model gave to that exact pack) but
    # nothing was billed for it this time, and cost_usd still carries the original
    # price. Tracked so the report can tell spend from replay.
    replayed: bool = False

    @property
    def verdict_agrees(self) -> bool:
        return self.actual_verdict is self.expected_verdict

    @property
    def right_for_the_right_reason(self) -> bool:
        """The gate call was right AND every criterion behind it was right.

        Tracked apart from verdict_agrees because a judge can block the right case
        while naming the wrong criterion, and the feedback it sends the builder is
        made of those criteria. A right verdict for a wrong reason produces a
        rebuild aimed at the wrong thing, which reads as a success in every metric
        that only counts verdicts.
        """
        return self.verdict_agrees and all(c.agrees for c in self.criteria)


@dataclass(frozen=True)
class EvalRun:
    """Everything one pass over the dataset produced."""

    model: str
    rules: str
    repeat: int
    cases: tuple[CaseScore, ...]
    scores: tuple[CriterionScore, ...]

    @property
    def total_cost_usd(self) -> Decimal:
        """What grading this dataset costs, replays priced as if they were paid."""
        return sum((c.cost_usd for c in self.cases), Decimal(0))

    @property
    def billed_cost_usd(self) -> Decimal:
        """What this invocation actually spent.

        These differ, and the gap is the point. Re-running the eval replays every
        identical call for free, so the second run of a dataset costs nothing and
        the total above still reads like a bill. Reporting only the total would be
        this project's own quiet-cost trap pointed the other way: overstating spend
        rather than hiding it, but wrong in the same way. Found on the first real
        --repeat run, where 8 of 40 samples replayed from the run before it.
        """
        return sum(
            (c.cost_usd for c in self.cases if not c.replayed), Decimal(0)
        )

    @property
    def replayed(self) -> int:
        """Samples the seam answered from cache.

        Not a flaw. A replayed sample is the answer that model gave to that exact
        pack, which is the same fact a fresh call would establish, at no cost. But
        it is not an independent draw, so it says nothing new about stability: five
        replays of one answer are one answer.
        """
        return sum(1 for c in self.cases if c.replayed)

    @property
    def unusable(self) -> int:
        """Samples where the judge could not produce a verdict at all.

        Not a scoring category, a sensor failure. Worth its own count because in
        production this is exactly what triggers the gate's fail-open path: the
        code passes with a note. A judge that is frequently unusable is a gate that
        is frequently absent, and nothing else in the report would show that.
        """
        return sum(1 for c in self.cases if c.error is not None)


def score_case(case: EvalCase, result: JudgeResult, sample: int, model: str) -> CaseScore:
    """Compare one judge verdict against the case's labels."""
    by_index = {lb.criterion: lb for lb in case.labels}
    call_key = result.record.idempotency_key if result.record else None
    scores = tuple(
        CriterionScore(
            case_name=case.name,
            provenance=case.provenance,
            source=case.source,
            criterion_index=i,
            criterion=v.criterion,
            expected=by_index[i].expect,
            actual=v.outcome,
            sample=sample,
            model=model,
            call_key=call_key,
            label_why=by_index[i].why,
            judge_reason=v.reason,
        )
        for i, v in enumerate(result.verdicts)
    )
    return CaseScore(
        case_name=case.name,
        provenance=case.provenance,
        sample=sample,
        expected_verdict=case.expected_verdict,
        actual_verdict=result.verdict,
        criteria=scores,
        cost_usd=result.cost_usd,
        replayed=bool(result.record and result.record.status == "replayed"),
    )


def run_evals(
    cases: tuple[EvalCase, ...],
    client: MeteredClient,
    model: str,
    repeat: int = 1,
) -> EvalRun:
    """Grade the judge against every case, `repeat` times each.

    Each sample runs at a distinct attempt_index, which is in the idempotency key,
    so sample 1 is a genuinely new paid call rather than a replay of sample 0. That
    is what makes repeats measure stability instead of measuring the cache.

    A JudgeError is caught, not raised: a judge whose reply cannot be parsed is a
    real and reportable behaviour of the sensor (in production it is what makes the
    gate fail open), and one unusable reply must not abandon the other seven cases.
    """
    if repeat < 1:
        raise EvalError(f"repeat must be at least 1, got {repeat}")

    case_scores: list[CaseScore] = []
    for case in cases:
        with tempfile.TemporaryDirectory(prefix=f"agentpipe-eval-{case.name}-") as tmp:
            repo = materialise(case, tmp)
            for sample in range(repeat):
                try:
                    result = run_judge(
                        case.ticket, repo, client, model, case.files,
                        attempt_index=sample,
                        task_ref=f"{TASK_REF_PREFIX}{case.name}",
                    )
                except JudgeError as exc:
                    # The verdict is lost but the call was still billed, so the
                    # cost comes off the record the error carries. Reporting $0
                    # here would make an unusable judge look like a free one, and
                    # a judge that is unusable often is both the most expensive
                    # and the most invisible failure this harness can have.
                    rec = exc.record
                    case_scores.append(CaseScore(
                        case_name=case.name, provenance=case.provenance,
                        sample=sample, expected_verdict=case.expected_verdict,
                        actual_verdict=None, criteria=(),
                        cost_usd=rec.cost_usd if rec else Decimal(0),
                        error=str(exc),
                        replayed=bool(rec and rec.status == "replayed"),
                    ))
                    continue
                case_scores.append(score_case(case, result, sample, model))

    return EvalRun(
        model=model,
        rules=rules_hash(),
        repeat=repeat,
        cases=tuple(case_scores),
        scores=tuple(s for c in case_scores for s in c.criteria),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_ROW_ORDER = (
    CriterionOutcome.SATISFIED,
    CriterionOutcome.NOT_SATISFIED,
    CriterionOutcome.UNCERTAIN,
)


def _cut(scores: tuple[CriterionScore, ...], label: str) -> str:
    """One line of counts. Counts only: see this module's docstring."""
    return (
        f"  {label:<13} graded {len(scores):<4} "
        f"agree {sum(1 for s in scores if s.agrees):<4} "
        f"false pass {sum(1 for s in scores if s.is_false_pass):<4} "
        f"false block {sum(1 for s in scores if s.is_false_block):<4} "
        f"abstain {sum(1 for s in scores if s.is_abstain)}"
    )


def report_evals(run: EvalRun) -> str:
    """The judge's report card, for a human, in counts."""
    n_cases = len({c.case_name for c in run.cases})
    lines = [
        f"judge eval   model {run.model}   rules {run.rules}",
        f"dataset      {n_cases} cases x {run.repeat} "
        f"sample{'s' if run.repeat > 1 else ''}   "
        f"{len(run.scores)} criteria graded",
        f"cost         ${run.billed_cost_usd} billed"
        + (
            f"   (${run.total_cost_usd} at full price; {run.replayed} "
            f"sample{'s' if run.replayed > 1 else ''} replayed from cache, "
            f"free and not an independent draw)"
            if run.replayed else ""
        ),
        "",
        "criterion agreement",
        "",
        f"  {'judge said':<16}{'label satisfied':>18}{'label not_satisfied':>22}",
    ]

    for actual in _ROW_ORDER:
        sat = sum(
            1 for s in run.scores
            if s.actual is actual and s.expected is CriterionOutcome.SATISFIED
        )
        notsat = sum(
            1 for s in run.scores
            if s.actual is actual and s.expected is CriterionOutcome.NOT_SATISFIED
        )
        flag = ""
        if actual is CriterionOutcome.SATISFIED:
            flag = "   <- false pass" if notsat else ""
        elif actual is CriterionOutcome.NOT_SATISFIED:
            flag = "   <- false block" if sat else ""
        lines.append(f"  {actual.value:<16}{sat:>18}{notsat:>22}{flag}")

    lines += [
        "",
        _cut(run.scores, "all"),
    ]
    for prov in PROVENANCE:
        cut = tuple(s for s in run.scores if s.provenance == prov)
        if cut:
            lines.append(_cut(cut, prov))

    right = sum(1 for c in run.cases if c.verdict_agrees)
    honest = sum(1 for c in run.cases if c.right_for_the_right_reason)
    lines += [
        "",
        "verdict agreement (would the gate have made the right call?)",
        "",
        f"  samples {len(run.cases)}   right verdict {right}   "
        f"right verdict for the right reason {honest}",
    ]
    if run.unusable:
        lines.append(
            f"  unusable replies {run.unusable}   "
            f"(in production these fail open: the code passes ungated)"
        )

    disagreements = [s for s in run.scores if not s.agrees]
    if disagreements:
        lines += ["", "disagreements", ""]
        # Worst first: a false pass is the failure that makes the gate pointless.
        for s in sorted(disagreements, key=lambda s: (not s.is_false_pass, s.case_name)):
            kind = (
                "false pass" if s.is_false_pass
                else "false block" if s.is_false_block
                else "abstain"
            )
            lines += [
                f"  [{kind}] {s.case_name}  criterion {s.criterion_index}: {s.criterion}",
                f"      label  {s.expected.value}: {s.label_why}",
                f"      judge  {s.actual.value}: {s.judge_reason}",
            ]
        lines += [
            "",
            "  Read every one of these twice. The judge may be wrong, or the label",
            "  may be. A dataset nobody re-examines becomes a confidently wrong",
            "  answer key, which is the failure PriceMap.from_env refuses to have.",
        ]

    broken = [c for c in run.cases if c.error]
    if broken:
        lines += ["", "unusable judge replies", ""]
        for c in broken:
            lines.append(f"  {c.case_name} sample {c.sample}: {c.error}")

    lines += [
        "",
        "Counts, not rates, on purpose: at this sample size one flipped verdict",
        "moves a percentage by double digits and still reads as a measurement.",
        "Nothing here is a pass mark. Whether --gate has earned its authority is",
        "your call, made while looking at these numbers.",
    ]
    return "\n".join(lines)


def report_dataset(cases: tuple[EvalCase, ...], with_tokens: bool = False) -> str:
    """What the dataset holds, and optionally what grading it would cost in tokens.

    The --dry-run output. Every validation a case can fail has already run by the
    time this prints, so a clean dry run means the dataset is loadable, complete,
    and its labels still match its tickets. That part is pure file reading and
    costs nothing.

    `with_tokens` is off by default, and that default was paid for. Estimating
    tokens means materialising every case, and materialising means a real `git
    init` plus `git add` per case: 40 subprocesses for a twenty-case dataset, on
    the command people run most often and expect to be free. Repeated runs left
    orphaned git processes on a Windows dev machine and helped take the box down.

    The lesson is not "avoid subprocesses". It is that an optional extra was
    welded onto the cheap path, so everyone paid for it whether or not they wanted
    it. Validation is the job here; the token estimate is a bonus, and bonuses opt
    in. Same shape as the test that grew slower with every case added: one
    decision, two symptoms.
    """
    lines = [f"dataset      {len(cases)} cases", ""]
    total = 0
    for case in cases:
        pack = None
        if with_tokens:
            with tempfile.TemporaryDirectory(prefix=f"agentpipe-dry-{case.name}-") as tmp:
                repo = materialise(case, tmp)
                pack = build_judge_pack(case.ticket, repo, case.files, case.criteria)
            total += pack.tokens
        lines.append(
            f"  {case.name:<26} {case.provenance:<12} "
            f"{len(case.criteria)} criteria  "
            f"expect {case.expected_verdict.value.upper():<5}"
            + (f" ~{pack.tokens:,} tokens" if pack else "")
        )
        # First sentence only. The full note lives in case.json and is meant to be
        # read there; a wall of wrapped prose here buries the numbers next to it.
        gist = case.note.split(". ")[0]
        if case.source:
            lines.append(f"      from {case.source}. {gist}.")
        elif gist:
            lines.append(f"      {gist}.")

    real = sum(1 for c in cases if c.provenance == "real")
    lines += ["", f"  {real} real, {len(cases) - real} constructed"]
    if with_tokens:
        lines.append(f"  ~{total:,} input tokens per sample, before the reply")
    else:
        lines.append("  (pass --tokens to estimate pack size; it materialises "
                     "every case, so it is not free)")
    lines += [
        "",
        "Validated: every case loads, every label covers its criterion exactly",
        "once, and every label's text still matches its ticket. Nothing was called.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", default="evals/cases", help="the case directory")
    ap.add_argument("--case", default=None, metavar="NAME",
                    help="grade one case only, by directory name")
    ap.add_argument("--model", default="gpt-5.4-mini")
    ap.add_argument("--models", default=None, metavar="PATH",
                    help="JSON file of role -> model overrides (or set "
                         "AGENTPIPE_MODELS). The 'judge' role is used.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="samples per case. Each runs at a distinct attempt_index, "
                         "so it is a new paid call, not a replay. Default 1: "
                         "repeats cost real money and only stability needs them.")
    ap.add_argument("--dry-run", action="store_true",
                    help="load and validate the dataset, call nothing, record "
                         "nothing, and spawn nothing")
    ap.add_argument("--tokens", action="store_true",
                    help="with --dry-run, also estimate each pack's size. Off by "
                         "default because it materialises every case, which is a "
                         "git init and a git add per case.")
    ap.add_argument("--no-record", action="store_true",
                    help="print the report but do not write judge_evals")
    args = ap.parse_args()

    try:
        cases = load_cases(args.cases, only=args.case)
    except EvalError as exc:
        # The cheapest possible failure, and a free one. Same discipline as the
        # ticket contract: a broken case costs nothing to find here and produces a
        # wrong number if found later.
        print(f"\n{exc}\n")
        print("Fix the case. This cost nothing.")
        return 1

    if args.dry_run:
        print()
        print(report_dataset(cases, with_tokens=args.tokens))
        print()
        return 0

    # Imports deferred to here so --dry-run needs neither a database nor a price
    # map. A validation pass should not require the production environment.
    from agentpipe.evalstore import PostgresEvalStore, record_eval_scores
    from agentpipe.telemetry import (
        PostgresCallStore,
        PriceMap,
        configure_tracing,
    )

    configure_tracing()
    model = ModelMap.from_env(base=args.model, path=args.models).for_role("judge")
    client = MeteredClient(store=PostgresCallStore(), prices=PriceMap.from_env(),
                           run_id=str(uuid.uuid4()))

    print(f"\nrun id: {client.run_id}")
    print(f"grading {len(cases)} cases x {args.repeat} on {model}. "
          f"This makes real calls.\n")

    run = run_evals(cases, client, model, repeat=args.repeat)
    print(report_evals(run))
    print()

    if not args.no_record:
        record_eval_scores(PostgresEvalStore(), client.run_id, run.scores, run.rules)
        print("recorded to judge_evals. Read it back:")
        print()
        print("  select * from judge_accuracy;")
        print("  select * from judge_stability;")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
