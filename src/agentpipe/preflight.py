"""Preflight and smoke test.

Run this before you trust a single number in the table.

Layer 0 promises the meter never fails a run. It does not promise the meter
never silently loses data, and on a free-tier database that pauses after a week
of quiet, those are very different promises. This script closes the gap: it
checks the store is actually writable, then proves the two guarantees that
matter with a real call.

    python -m agentpipe.preflight          # checks only, no API call, free
    python -m agentpipe.preflight --smoke  # + two real calls, costs ~$0.0001
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from agentpipe.telemetry import (
    MeteredClient,
    PostgresCallStore,
    PriceMap,
    Usage,
)

OK = "  ok   "
FAIL = " FAIL  "
WARN = " warn  "


def _check(label: str, fn) -> bool:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] {label}\n         {type(exc).__name__}: {exc}")
        return False
    print(f"[{OK}] {label}" + (f"\n         {detail}" if detail else ""))
    return True


def check_env() -> str:
    missing = [
        v for v in ("AGENTPIPE_DSN", "AGENTPIPE_PRICES", "OPENAI_API_KEY")
        if not os.environ.get(v)
    ]
    if missing:
        raise RuntimeError(f"not set: {', '.join(missing)}")
    return ""


def check_prices() -> str:
    prices = PriceMap.from_env()
    unpriced = [
        m for m, e in prices._prices.items()
        if not m.startswith("_") and e.get("input") is None
    ]
    if unpriced:
        raise RuntimeError(
            f"models with null prices: {', '.join(unpriced)}. "
            "Fill these in from your provider's pricing page. A cost table "
            "built on nulls reports zero and looks like it works."
        )
    return f"{len([m for m in prices._prices if not m.startswith('_')])} models priced"


def check_store_readable() -> str:
    """The paused-project check.

    A paused Supabase project refuses connections outright, so this fails loudly
    here rather than quietly inside _safe_record an hour into a run.
    """
    store = PostgresCallStore()
    store.find("preflight-probe-does-not-exist")
    return "connected"


def check_store_writable() -> str:
    """Read access is not write access. Prove the insert actually lands."""
    from decimal import Decimal

    from agentpipe.telemetry import CallRecord

    store = PostgresCallStore()
    key = f"preflight-{uuid.uuid4()}"
    probe = CallRecord(
        run_id=str(uuid.uuid4()),
        idempotency_key=key,
        role="builder",
        attempt_kind="implement",
        attempt_index=0,
        model="preflight-probe",
        usage=Usage(),
        cost_usd=Decimal(0),
        status="error",
        duration_ms=0,
        task_ref="PREFLIGHT",
        error="preflight write probe, safe to ignore",
    )
    store.record(probe)
    if store.find(key) is None:
        raise RuntimeError(
            "wrote a row and could not read it back. Check that schema.sql "
            "has been applied to this database."
        )
    return "insert confirmed"


def smoke(model: str) -> None:
    print("\n--- smoke test: two identical calls, one should be free\n")

    client = MeteredClient(store=PostgresCallStore(), prices=PriceMap.from_env())
    messages = [{"role": "user", "content": "Reply with exactly one word: ok"}]
    task = f"SMOKE-{uuid.uuid4().hex[:6]}"

    first = client.call(
        messages=messages, model=model, role="builder",
        attempt_kind="implement", attempt_index=1, task_ref=task,
    )
    print(f"  call 1  status={first.status}  "
          f"in={first.usage.input_tokens} out={first.usage.output_tokens} "
          f"ratio={first.usage.ratio:.1f} cost=${first.cost_usd}")

    second = client.call(
        messages=messages, model=model, role="builder",
        attempt_kind="implement", attempt_index=1, task_ref=task,
    )
    print(f"  call 2  status={second.status}  "
          f"in={second.usage.input_tokens} out={second.usage.output_tokens} "
          f"cost=${second.cost_usd}")

    print()
    if second.status != "replayed":
        print(f"[{FAIL}] call 2 hit the API. Idempotency is not working, and you "
              f"are paying twice for identical calls.")
        sys.exit(1)
    print(f"[{OK}] call 2 was replayed from the store. You paid once.")

    if first.cost_usd == 0:
        print(f"[{WARN}] cost came back as $0. Either '{model}' is missing from "
              f"your price map, or its prices are null.")

    print("\n--- now check the table\n")
    print("    select * from ratio_by_role;\n")
    print("    One row. That is your baseline. Andrew's is 700.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true",
                    help="make two real API calls (costs a fraction of a cent)")
    ap.add_argument("--model", default="gpt-5.1-mini",
                    help="model for the smoke test")
    args = ap.parse_args()

    print("--- preflight\n")
    checks = [
        ("environment variables set", check_env),
        ("price map loads and has no nulls", check_prices),
        ("store is reachable (is the project paused?)", check_store_readable),
        ("store is writable and schema applied", check_store_writable),
    ]
    if not all(_check(label, fn) for label, fn in checks):
        print("\nPreflight failed. Fix the above before trusting any number "
              "this thing reports.")
        return 1

    print("\nPreflight passed.")

    if args.smoke:
        smoke(args.model)
    else:
        print("\nRun with --smoke to make two real calls and prove idempotency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
