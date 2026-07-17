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

from agentpipe.builder import BuildResult, run_builder
from agentpipe.checks import CheckResult, Outcome, Verdict, assess, run_checks
from agentpipe.patch import PatchError, apply_edits, parse_edits
from agentpipe.repo import Repo
from agentpipe.telemetry import CallRecord, MeteredClient
from agentpipe.ticket import Ticket

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
    # Mutable across attempts.
    attempt: int
    feedback: Optional[str]
    build_error: Optional[str]
    # results accumulates: each build appends one, so operator.add not replace.
    results: Annotated[list[BuildResult], operator.add]
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

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((r.cost_usd for r in self.results), Decimal(0))


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
        return {"verdict": "pass", "validation": results,
                "acceptance_warning": _acceptance_disagreement(state["ticket"], state["repo"])}

    feedback = _combine_output(results, state["feedback_max_chars"])
    return _decide_fail(state, feedback, results)


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
) -> LoopResult:
    """Run the build/validate/retry loop until it passes, gives up, or breaks.

    Writes to the working tree on every attempt, because validation runs against
    real files. Run it on a scratch repo or a clean git state you can reset.

    `resume` continues a crashed run: the caller builds `client` with the run_id
    it wants to continue, and the loop recovers where that run got to (see
    `_resume`) rather than starting over.
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
        "attempt": start_attempt,
        "feedback": feedback,
        "build_error": None,
        "results": [],
        "verdict": "",
        "validation": (),
        "acceptance_warning": None,
    }
    # Each attempt is two supersteps (build, validate). Give the graph headroom
    # above that so our own "exhausted" verdict is what stops the loop, not
    # LangGraph's recursion_limit raising GraphRecursionError from underneath us.
    final = app.invoke(initial, config={"recursion_limit": 2 * max_attempts + 5})

    return LoopResult(
        verdict=final["verdict"],
        attempts=final["attempt"],
        results=tuple(final["results"]),
        validation=final["validation"],
        acceptance_warning=final.get("acceptance_warning"),
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
    """
    if prior.status not in ("ok", "replayed") or not prior.content:
        return
    try:
        edits = parse_edits(prior.content)
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

    if result.acceptance_warning:
        lines += ["", f"WARNING: {result.acceptance_warning}"]

    return "\n".join(lines)
