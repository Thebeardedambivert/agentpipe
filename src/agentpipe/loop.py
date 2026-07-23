"""The loop.

Build, validate, and if validation fails, try again with the failure in hand.
This is Layer 3, and it is the first time the workflow reacts to its own output.

Three ideas from PLAN.md meet here:

1. The workflow owns the loop. The graph decides when to retry and when to stop.
   The model only ever gets a pack and returns a pack.
2. Rebuild, never accumulate. Each retry calls run_builder fresh against the repo
   as it is now, with one thing added: the last failure, appended last so the
   cached prefix survives. Attempt 4 does not carry attempts 1 through 3.
3. Validation output is the truth. The model's claim that it worked is not
   evidence; the exit code of the ticket's validation commands is.

The graph is deliberately tiny: two nodes and one conditional edge. The edge is
the whole point, and the reason LangGraph was deferred to here rather than
introduced at Layer 2 as a single node with nothing to decide.

Durability, stated honestly: this loop runs in-process. Individual calls are
idempotent at the seam, so a re-run does not pay twice for an identical call, but
a process killed mid-loop restarts rather than resuming. The table-derived
attempt counter (Stage 2) and Temporal (Layer 7) are what make it durable. The
residual window, a crash between applying a patch and recording the call, is
A1.5's, and it is Layer 7's to close.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry import trace as otel_trace

from agentpipe.builder import BuildResult, run_builder
from agentpipe.checks import CheckResult, Outcome, Verdict, assess, run_checks
from agentpipe.judge import (
    CriterionOutcome,
    JudgeError,
    JudgeResult,
    JudgeVerdict,
    run_judge,
)
from agentpipe.patch import PatchError, apply_edits, parse_edits
from agentpipe.repo import Repo
from agentpipe.telemetry import CallRecord, MeteredClient
from agentpipe.ticket import Ticket

_tracer = otel_trace.get_tracer("agentpipe.loop")

# How much of a validation failure to feed back into the next attempt. A ceiling,
# not a tuned number: enough that the model sees the real error, bounded so a
# runaway traceback cannot balloon the pack. What would settle it: run real
# failing tickets and look at whether the model fixes the problem when given the
# last N chars versus more. Coded as a parameter, not a constant, so it can be
# disproven.
FEEDBACK_MAX_CHARS = 2_000


class LoopState(TypedDict):
    # Config, constant for the run.
    ticket: Ticket
    repo: Repo
    client: MeteredClient
    model: str
    max_attempts: int
    feedback_max_chars: int
    # The eval gate (Layer 6 Stage 2). Off by default: with gate=False the loop
    # behaves exactly as Layer 3 built it. On, the judge gets a second say after
    # tests pass, and a BLOCK is fed back to the builder like a test failure,
    # bounded by the same max_attempts.
    gate: bool
    judge_model: str
    # Mutable across attempts.
    attempt: int
    feedback: Optional[str]
    build_error: Optional[str]
    # results accumulates: each build appends one, so operator.add not replace.
    results: Annotated[list[BuildResult], operator.add]
    # judges accumulates one per gated attempt, so the run's total cost is honest
    # and the last verdict is reportable.
    judges: Annotated[list[JudgeResult], operator.add]
    verdict: str  # "" | pass | retry | exhausted | blocked
    validation: tuple[CheckResult, ...]
    acceptance_warning: Optional[str]


@dataclass(frozen=True)
class LoopResult:
    """What the loop did, and what it cost, for a human and for a test."""

    verdict: str  # pass | exhausted | blocked
    attempts: int
    results: tuple[BuildResult, ...]
    validation: tuple[CheckResult, ...]
    acceptance_warning: Optional[str] = None
    judges: tuple[JudgeResult, ...] = ()  # one per gated attempt, when gating is on

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    @property
    def judge(self) -> Optional[JudgeResult]:
        """The last judge verdict, or None if the run was not gated."""
        return self.judges[-1] if self.judges else None

    @property
    def total_cost_usd(self) -> Decimal:
        # Include the judge's calls: with gating on they are a real part of the
        # run's bill, and hiding them would be the quiet-cost trap this project
        # exists to refuse.
        build = sum((r.cost_usd for r in self.results), Decimal(0))
        judge = sum((j.cost_usd for j in self.judges), Decimal(0))
        return build + judge


def _build_node(state: LoopState) -> dict[str, Any]:
    """One attempt: build a pack, call the model, apply the patch.

    Never raises. A PatchError (the reply could not be turned into files) is a
    failed attempt the model can recover from with a better-formed reply, so it
    becomes feedback rather than a crash. That is different from a broken
    validation command, which the model cannot fix and which stops the run.
    """
    try:
        result = run_builder(
            state["ticket"],
            state["repo"],
            state["client"],
            state["model"],
            attempt=state["attempt"],
            feedback=state["feedback"],
            dry_run=False,
        )
        return {"results": [result], "build_error": None}
    except PatchError as exc:
        return {"build_error": f"your previous reply could not be applied: {exc}"}


def _validate_node(state: LoopState) -> dict[str, Any]:
    """Run the ticket's validation and decide what happens next.

    The exit-code contract is checks.py's: 0 pass, 1 fail, anything else broken.
    A broken validation command stops the run loudly; only a genuine test failure
    (exit 1) is fed back and retried.
    """
    if state.get("build_error"):
        # The attempt produced nothing applicable. There is nothing new to
        # validate, so route it straight to the retry/exhaust decision with the
        # parse error as the feedback.
        return _decide_fail(state, state["build_error"], ())

    results = run_checks(state["ticket"].validation, state["repo"].root)

    if any(r.outcome is Outcome.ERROR for r in results):
        return {"verdict": "blocked", "validation": results}

    if all(r.outcome is Outcome.PASS for r in results):
        if not state["gate"]:
            return {"verdict": "pass", "validation": results,
                    "acceptance_warning": _acceptance_disagreement(state["ticket"], state["repo"])}
        # Tests pass. When gating, the judge gets the second say: it can send a
        # wrong-but-passing patch back to the builder.
        return _gate(state, results)

    feedback = _combine_output(results, state["feedback_max_chars"])
    return _decide_fail(state, feedback, results)


def _gate(state: LoopState, results: tuple[CheckResult, ...]) -> dict[str, Any]:
    """The eval gate. Judge the passing code; a BLOCK retries with its reasons.

    Fail open: if the judge itself cannot produce a verdict (a malformed reply),
    the code passes without it. A broken sensor must not block otherwise-passing
    work, the same principle as the meter never failing a run. The judge is not yet
    measured (that is Stage 3), so it gets to stop and retry, never to fail the run
    on its own malfunction.
    """
    written = state["results"][-1].written if state["results"] else ()
    try:
        judge = run_judge(
            state["ticket"], state["repo"], state["client"],
            state["judge_model"], written,
        )
    except JudgeError as exc:
        return {"verdict": "pass", "validation": results,
                "acceptance_warning": f"gate skipped: the judge's reply was unusable ({exc})"}

    if judge.verdict is JudgeVerdict.BLOCK:
        out = _decide_fail(state, _judge_feedback(judge), results)
        out["judges"] = [judge]
        return out

    # PASS or UNGUARDED: a real pass, with the judge recorded for cost and report.
    return {"verdict": "pass", "validation": results, "judges": [judge],
            "acceptance_warning": _acceptance_disagreement(state["ticket"], state["repo"])}


def _judge_feedback(judge: JudgeResult) -> str:
    """Turn a BLOCK into feedback the builder can act on: the criteria it missed."""
    lines = [
        "Your tests passed, but the code was judged against the ticket's acceptance "
        "criteria and these are not yet met. Fix them:",
    ]
    for v in judge.verdicts:
        if v.outcome is not CriterionOutcome.SATISFIED:
            lines.append(f"- {v.criterion} ({v.outcome.value}): {v.reason}")
    return "\n".join(lines)


def _decide_fail(
    state: LoopState, feedback: str, results: tuple[CheckResult, ...]
) -> dict[str, Any]:
    """Retry with the failure in hand, or stop because the budget is spent."""
    if state["attempt"] >= state["max_attempts"]:
        return {"verdict": "exhausted", "validation": results}
    return {
        "verdict": "retry",
        "attempt": state["attempt"] + 1,
        "feedback": feedback,
        "validation": results,
    }


def _acceptance_disagreement(ticket: Ticket, repo: Repo) -> Optional[str]:
    """Validation passed. Do the ticket's acceptance checks agree?

    A warning, not a gate. Validation (pytest) passing does not prove this
    ticket's specific work was done; that is the stale-ticket lesson. If the
    ticket carries acceptance checks and they do not pass, the loop has produced
    green tests over unfinished work. Surfacing it here makes the gap visible;
    Layer 6's judge is what actually closes it.
    """
    if not ticket.checks:
        return None
    decision = assess(ticket, repo.root)
    if decision.verdict is Verdict.SATISFIED:
        return None
    return f"validation passed but acceptance checks did not: {decision.reason}"


def _router(state: LoopState) -> str:
    """The conditional edge. Retry loops back to build; everything else ends."""
    return "build" if state["verdict"] == "retry" else END


def _combine_output(results: tuple[CheckResult, ...], max_chars: int) -> str:
    parts = [
        f"$ {r.command}\n{r.output}"
        for r in results
        if r.outcome is not Outcome.PASS
    ]
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        # Keep the tail: the actual assertion is usually at the end, and the head
        # is boilerplate. Mark the cut so nobody reads a fragment as the whole.
        text = "...(truncated)...\n" + text[-max_chars:]
    return text


def _compile_graph():
    graph = StateGraph(LoopState)
    graph.add_node("build", _build_node)
    graph.add_node("validate", _validate_node)
    graph.add_edge(START, "build")
    graph.add_edge("build", "validate")
    graph.add_conditional_edges("validate", _router, {"build": "build", END: END})
    return graph.compile()


def run_loop(
    ticket: Ticket,
    repo: Repo,
    client: MeteredClient,
    model: str,
    max_attempts: int = 3,
    feedback_max_chars: int = FEEDBACK_MAX_CHARS,
    resume: bool = False,
    gate: bool = False,
    judge_model: Optional[str] = None,
) -> LoopResult:
    """Run the build/validate/retry loop until it passes, gives up, or breaks.

    Writes to the working tree on every attempt, because validation runs against
    real files. Run it on a scratch repo or a clean git state you can reset.

    `resume` continues a crashed run: the caller builds `client` with the run_id
    it wants to continue, and the loop recovers where that run got to (see
    `_resume`) rather than starting over.

    `gate` turns on the Layer 6 eval gate: after tests pass, the judge grades the
    ticket's semantic acceptance criteria, and a BLOCK is fed back to the builder
    like a test failure, bounded by the same max_attempts. `judge_model` chooses the
    judge's model (defaults to `model`). Off by default, so the loop is unchanged
    for every existing caller. Resume does not re-judge; the gate is for fresh runs.
    """
    start_attempt = 1
    feedback: Optional[str] = None

    if resume:
        recovered = _resume(ticket, repo, client, max_attempts, feedback_max_chars)
        if isinstance(recovered, LoopResult):
            return recovered  # the recovered state already decides the outcome
        start_attempt, feedback = recovered

    app = _compile_graph()
    initial: LoopState = {
        "ticket": ticket,
        "repo": repo,
        "client": client,
        "model": model,
        "max_attempts": max_attempts,
        "feedback_max_chars": feedback_max_chars,
        "gate": gate,
        "judge_model": judge_model or model,
        "attempt": start_attempt,
        "feedback": feedback,
        "build_error": None,
        "results": [],
        "judges": [],
        "verdict": "",
        "validation": (),
        "acceptance_warning": None,
    }
    # One span per run, so a run's per-call spans nest under it and share a
    # trace_id: the tree PLAN.md said the trace would become at Layer 3. With no
    # tracer configured this is a no-op and costs nothing.
    with _tracer.start_as_current_span("agentpipe.run") as span:
        span.set_attribute("agentpipe.task_ref", ticket.ref)
        span.set_attribute("agentpipe.max_attempts", max_attempts)
        # Each attempt is two supersteps (build, validate). Give the graph
        # headroom above that so our own "exhausted" verdict is what stops the
        # loop, not LangGraph's recursion_limit raising GraphRecursionError.
        final = app.invoke(initial, config={"recursion_limit": 2 * max_attempts + 5})

    return LoopResult(
        verdict=final["verdict"],
        attempts=final["attempt"],
        results=tuple(final["results"]),
        validation=final["validation"],
        acceptance_warning=final.get("acceptance_warning"),
        judges=tuple(final.get("judges", [])),
    )


def _resume(ticket, repo, client, max_attempts, feedback_max_chars):
    """Prepare a crashed run to continue.

    The counter is a hint; the repo is the truth. We recover the last recorded
    attempt's work from its stored reply (free), then let validation against the
    real files decide, never the counter. Returns a finished LoopResult when the
    recovered state already passes or the budget is spent, otherwise the
    (start_attempt, feedback) to continue with.
    """
    prior = client.latest_attempt()
    if prior is None:
        return 1, None  # nothing recorded for this run; a fresh start

    _reapply_recorded_reply(prior, ticket, repo)

    results = run_checks(ticket.validation, repo.root)
    if all(r.outcome is Outcome.PASS for r in results):
        # The crash happened after the fix landed. Nothing left to do, nothing to
        # pay: the repo already satisfies the ticket.
        return LoopResult(
            "pass", prior.attempt_index, (), results,
            _acceptance_disagreement(ticket, repo),
        )

    start_attempt = prior.attempt_index + 1
    if start_attempt > max_attempts:
        return LoopResult("exhausted", prior.attempt_index, (), results, None)

    # Rebuild the feedback from the real repo, not from memory we no longer have.
    return start_attempt, _combine_output(results, feedback_max_chars)


def _reapply_recorded_reply(prior: CallRecord, ticket: Ticket, repo: Repo) -> None:
    """Re-apply a recorded attempt's stored reply, for free.

    This closes the cost side of the resume window. An attempt can be recorded
    (billed) and then lost before its files reach disk. Because the reply was
    stored (migration 002), we re-apply it here instead of paying the model to
    redo it. Idempotent: if the work already landed, this rewrites identical
    bytes. An attempt whose reply was not applicable (the model returned prose)
    has nothing to recover, and that is fine; the loop rebuilds from real state.

    That tolerance now covers a second case, and it is why this is a `try` rather
    than a bare call. Replies recorded before the search/replace change are in the
    old whole-file format, which the parser refuses outright. Those runs lose the
    free re-apply and fall back to rebuilding from the repo, which is exactly what
    this function does when it cannot recover anything. Old rows degrade; nothing
    crashes.
    """
    if prior.status not in ("ok", "replayed") or not prior.content:
        return
    try:
        edits = parse_edits(prior.content, repo)
        apply_edits(repo, edits, allowed=ticket.files_hint or None, dry_run=False)
    except PatchError:
        return


def report_loop(result: LoopResult) -> str:
    """The whole run in one block a human can read."""
    headline = {
        "pass": "PASSED",
        "exhausted": "GAVE UP (hit the attempt limit)",
        "blocked": "STOPPED (a validation command could not run)",
    }.get(result.verdict, result.verdict.upper())

    lines = [
        f"loop       {headline}",
        f"attempts   {result.attempts}",
        f"cost       ${result.total_cost_usd}",
    ]
    for i, r in enumerate(result.results, start=1):
        u = r.record.usage
        lines.append(
            f"  attempt {i}: {r.record.attempt_kind:<16} "
            f"in={u.input_tokens:,} out={u.output_tokens:,} "
            f"cache={u.cache_hit_rate:.0%} ${r.cost_usd}"
        )

    if result.verdict != "pass":
        lines += ["", "last validation output:"]
        for r in result.validation:
            if r.outcome is not Outcome.PASS:
                lines.append(f"  [{r.outcome.value}] {r.command}")
                if r.output:
                    lines.append("    " + r.output.replace("\n", "\n    "))

    # The gate's verdict, when the run was gated. On a BLOCK (whether the loop went
    # on to pass or exhausted) show which criteria it kept flagging.
    if result.judge is not None:
        j = result.judge
        lines += ["", f"judge      {j.verdict.value.upper()}   ${j.cost_usd}"]
        if j.verdict is not JudgeVerdict.PASS:
            for v in j.verdicts:
                if v.outcome is not CriterionOutcome.SATISFIED:
                    lines.append(f"  [{v.outcome.value}] {v.criterion}: {v.reason}")

    if result.acceptance_warning:
        lines += ["", f"WARNING: {result.acceptance_warning}"]

    return "\n".join(lines)
