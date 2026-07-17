"""Run a ticket.

    python -m agentpipe.run tickets/TASK-1.md              # dry run, safe
    python -m agentpipe.run tickets/TASK-1.md --apply      # writes files

Dry run is the default because writing to a working tree should be a decision,
not a side effect of typing a command wrong.
"""

from __future__ import annotations

import argparse
import sys

from agentpipe.builder import report, run_builder
from agentpipe.checks import Verdict, assess
from agentpipe.loop import report_loop, run_loop
from agentpipe.patch import PatchError
from agentpipe.repo import Repo, RepoError
from agentpipe.telemetry import (
    MeteredClient,
    PostgresCallStore,
    PriceMap,
    configure_tracing,
)
from agentpipe.ticket import Ticket, TicketError


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticket", help="path to a ticket markdown file")
    ap.add_argument("--repo", default=".", help="repository root")
    ap.add_argument("--model", default="gpt-5.4-mini")
    ap.add_argument("--max-files", type=int, default=5)
    ap.add_argument("--max-output", type=int, default=None,
                    help="output token budget. Derived from file sizes if unset.")
    ap.add_argument("--apply", action="store_true", help="actually write files")
    ap.add_argument("--max-attempts", type=int, default=1,
                    help="more than 1 runs the validation loop, which writes "
                         "the working tree on every attempt")
    ap.add_argument("--resume", default=None, metavar="RUN_ID",
                    help="resume a crashed loop by its run id "
                         "(printed when a loop starts)")
    args = ap.parse_args()

    # Make trace ids real for this run. No-op unless the SDK is present, and it
    # is; safe to call once here at the entry point.
    configure_tracing()

    try:
        ticket = Ticket.from_file(args.ticket)
    except TicketError as exc:
        # The cheapest possible failure. No model call, no database write, no
        # waiting. This is the whole reason the ticket contract is strict.
        print(f"\n{exc}\n")
        print("Fix the ticket. This cost nothing.")
        return 1

    try:
        repo = Repo(args.repo)
    except RepoError as exc:
        print(f"repo error: {exc}")
        return 1

    # The cheapest question, asked before the expensive one: is this already
    # done? A ticket whose acceptance checks all pass is stale, and the failure
    # this gate exists to refuse is paying a model to rewrite a correct file.
    # No model, no database, just the checks the ticket declared.
    decision = assess(ticket, repo.root)
    if decision.verdict is Verdict.BROKEN:
        print(f"\n{decision.reason}\n")
        for r in decision.results:
            if r.outcome.name == "ERROR":
                print(f"  broken check: {r.command}")
                print(f"    exit {r.exit_code}: {r.output}")
        print("\nFix the check. This cost nothing.")
        return 1
    if decision.verdict is Verdict.SATISFIED:
        print(f"\n{decision.reason}:")
        for r in decision.results:
            print(f"  ok: {r.command}")
        print("\nNothing to do. This cost nothing.")
        return 0
    if not ticket.checks:
        # Say the gate was skipped, out loud. A staleness check that silently
        # does nothing is the same trap as a test that silently skips.
        print("note: this ticket has no acceptance checks, so staleness is unguarded.")

    client = MeteredClient(store=PostgresCallStore(), prices=PriceMap.from_env(),
                           run_id=args.resume)

    if args.max_attempts > 1:
        # The loop writes on every attempt, because validation runs against real
        # files. Writing is a decision, so it is opt-in via --max-attempts and
        # announced rather than silent.
        verb = "resuming" if args.resume else "running"
        print(f"\n{verb} up to {args.max_attempts} attempts; this writes to the "
              f"working tree on each one.")
        # Print the run id so a crash can be resumed with --resume.
        print(f"run id: {client.run_id}  (resume with --resume {client.run_id})\n")
        loop_result = run_loop(
            ticket, repo, client, args.model,
            max_attempts=args.max_attempts, resume=bool(args.resume),
        )
        print(report_loop(loop_result))
        print()
        return 0 if loop_result.ok else 1

    try:
        result = run_builder(
            ticket, repo, client, args.model,
            max_files=args.max_files,
            max_output_tokens=args.max_output,
            dry_run=not args.apply,
        )
    except PatchError as exc:
        # The call was made and paid for. The reply was unusable. Both facts
        # are worth stating, because the second one is what Layer 3 will retry
        # and the first one is already in the table.
        print(f"\nthe model's reply could not be used:\n{exc}\n")
        print("The call was still made and billed. Check model_calls.")
        print()
        print("  select * from output_shape limit 3;")
        print()
        print("If finish_reason is 'length', the model was cut off mid-thought.")
        print("If reasoning_tokens is high and answer_tokens is 0, it thought")
        print("and never spoke. If output_tokens is near 0, it had nothing to")
        print("say, which on a stale ticket is the correct answer.")
        return 1

    print()
    print(report(result, repo, dry_run=not args.apply))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
