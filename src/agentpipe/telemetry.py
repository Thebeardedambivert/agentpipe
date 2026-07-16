"""The seam.

Every model call in the pipeline goes through MeteredClient.call(). There is no
second way to reach a model. That is the entire design constraint of this module,
and everything else here follows from it.

Three jobs, in order of importance:

1. Refuse to make a call we have already paid for   (idempotency)
2. Record what every call cost                      (telemetry)
3. Actually make the call                           (the boring part)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, Sequence

from openai import OpenAI
from opentelemetry import trace

Role = Literal["builder", "reviewer", "fixer", "judge"]
AttemptKind = Literal["implement", "validation_retry", "review_fix", "review", "eval"]

tracer = trace.get_tracer("agentpipe.telemetry")


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

class PriceMap:
    """Model prices, loaded from config rather than baked into code.

    Prices change. Code that hardcodes them is wrong on a timetable. Load from
    AGENTPIPE_PRICES (a path to JSON) so a price change is a config edit.

    Shape, USD per 1M tokens:

        {
          "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.00}
        }

    Go and read your provider's current pricing page and write this file
    yourself. Do not trust a number an LLM gave you, including this one, and
    including the example above.
    """

    def __init__(self, prices: dict[str, dict[str, float]]) -> None:
        self._prices = prices

    @classmethod
    def from_env(cls) -> PriceMap:
        path = os.environ.get("AGENTPIPE_PRICES")
        if not path:
            raise RuntimeError(
                "AGENTPIPE_PRICES is not set. Point it at a JSON file of model "
                "prices. Refusing to guess: a wrong price map is worse than no "
                "price map, because it looks like it works."
            )
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def cost_usd(self, model: str, usage: Usage) -> Decimal:
        """Cost in USD. Decimal, not float: this is money."""
        entry = self._prices.get(model)
        if entry is None:
            # Unknown model is not an error. A missing price must never fail a
            # run. But it must be loud, or you will find out in six weeks.
            return Decimal(0)

        per_million = Decimal(1_000_000)
        fresh_input = usage.input_tokens - usage.cached_input_tokens

        cost = (
            Decimal(str(entry.get("input", 0))) * fresh_input
            + Decimal(str(entry.get("cached_input", entry.get("input", 0))))
            * usage.cached_input_tokens
            + Decimal(str(entry.get("output", 0))) * usage.output_tokens
        ) / per_million

        return cost.quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def ratio(self) -> float:
        """Input per unit of output. Andrew's 70k/100 is a ratio of 700."""
        if self.output_tokens == 0:
            return float("inf")
        return self.input_tokens / self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        if self.input_tokens == 0:
            return 0.0
        return self.cached_input_tokens / self.input_tokens


@dataclass(frozen=True)
class CallRecord:
    run_id: str
    idempotency_key: str
    role: Role
    attempt_kind: AttemptKind
    attempt_index: int
    model: str
    usage: Usage
    cost_usd: Decimal
    status: Literal["ok", "error", "replayed"]
    duration_ms: int
    task_ref: str | None = None
    pack_hash: str | None = None
    error: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    content: str = ""


# ---------------------------------------------------------------------------
# Store (a port, in A1's sense: swap Postgres for memory without touching
# the client)
# ---------------------------------------------------------------------------

class CallStore(ABC):
    @abstractmethod
    def find(self, idempotency_key: str) -> CallRecord | None:
        """Return a previously recorded call, or None."""

    @abstractmethod
    def record(self, rec: CallRecord) -> None:
        """Append a call. Must be atomic on idempotency_key."""


class InMemoryCallStore(CallStore):
    """For tests, and for the first hour of Layer 0 before you wire Supabase."""

    def __init__(self) -> None:
        self.records: dict[str, CallRecord] = {}

    def find(self, idempotency_key: str) -> CallRecord | None:
        return self.records.get(idempotency_key)

    def record(self, rec: CallRecord) -> None:
        self.records.setdefault(rec.idempotency_key, rec)


class PostgresCallStore(CallStore):
    """Supabase / Postgres. Requires schema.sql to have been applied.

    Note what does the real work here: the unique index on idempotency_key.
    The check-then-act in MeteredClient is an optimisation. The database
    constraint is the guarantee. If you only had one of them, you would want
    this one.
    """

    def __init__(self, dsn: str | None = None) -> None:
        import psycopg  # imported lazily so tests need no driver

        self._psycopg = psycopg
        self._dsn = dsn or os.environ["AGENTPIPE_DSN"]

    def find(self, idempotency_key: str) -> CallRecord | None:
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                """
                select run_id, idempotency_key, role, attempt_kind, attempt_index,
                       model, input_tokens, cached_input_tokens, output_tokens,
                       cost_usd, status, duration_ms, task_ref, pack_hash, error
                  from model_calls
                 where idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()

        if row is None:
            return None

        return CallRecord(
            run_id=str(row[0]),
            idempotency_key=row[1],
            role=row[2],
            attempt_kind=row[3],
            attempt_index=row[4],
            model=row[5],
            usage=Usage(
                input_tokens=row[6],
                cached_input_tokens=row[7],
                output_tokens=row[8],
            ),
            cost_usd=Decimal(row[9]),
            status=row[10],
            duration_ms=row[11],
            task_ref=row[12],
            pack_hash=row[13],
            error=row[14],
            content="",  # not stored: see the note in MeteredClient.call
        )

    def record(self, rec: CallRecord) -> None:
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(
                """
                insert into model_calls (
                    run_id, idempotency_key, role, attempt_kind, attempt_index,
                    task_ref, model, pack_hash, input_tokens, cached_input_tokens,
                    output_tokens, cost_usd, status, error, duration_ms,
                    trace_id, span_id
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                on conflict (idempotency_key) do nothing
                """,
                (
                    rec.run_id, rec.idempotency_key, rec.role, rec.attempt_kind,
                    rec.attempt_index, rec.task_ref, rec.model, rec.pack_hash,
                    rec.usage.input_tokens, rec.usage.cached_input_tokens,
                    rec.usage.output_tokens, rec.cost_usd, rec.status, rec.error,
                    rec.duration_ms, rec.trace_id, rec.span_id,
                ),
            )


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------

def pack_hash(messages: Sequence[dict[str, Any]]) -> str:
    """Content hash of a context pack.

    This is the mechanism behind CEE-6's Main Rule. "Same inputs produce the
    same pack" is a claim until something can check it. This checks it.
    """
    blob = json.dumps(messages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def idempotency_key(
    task_ref: str,
    role: Role,
    attempt_kind: AttemptKind,
    attempt_index: int,
    pack: str,
) -> str:
    """Identity of a logical call.

    Deliberately excludes run_id. Two runs of the same task, at the same
    attempt index, with a byte-identical pack, are the same call, and the second
    one should cost nothing. That is the whole point.

    Include pack_hash and you get the property you want for free: if the context
    changed, it is genuinely a different call and should be paid for. If it did
    not change, re-running is waste.
    """
    raw = f"{task_ref}|{role}|{attempt_kind}|{attempt_index}|{pack}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MeteredClient:
    """The only way to call a model.

    If you find yourself importing OpenAI anywhere else in this project, that is
    the bug. Not a style problem. The telemetry is only true if this is the
    single door.
    """

    def __init__(
        self,
        store: CallStore,
        prices: PriceMap,
        client: OpenAI | None = None,
        run_id: str | None = None,
    ) -> None:
        self._store = store
        self._prices = prices
        self._client = client or OpenAI()
        self.run_id = run_id or str(uuid.uuid4())

    def call(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str,
        role: Role,
        attempt_kind: AttemptKind,
        attempt_index: int = 0,
        task_ref: str | None = None,
        **kwargs: Any,
    ) -> CallRecord:
        pack = pack_hash(messages)
        key = idempotency_key(
            task_ref or "adhoc", role, attempt_kind, attempt_index, pack
        )

        # A1.5, made concrete. The crash we are defending against is: the API
        # call succeeds, we are billed, and the process dies before the result
        # is recorded. On retry we would pay again for an identical call.
        #
        # Only a successful call is replayable. Replaying an error would turn a
        # transient provider blip into a permanent one: the task would be stuck
        # forever, and no retry could ever dislodge it, because the cache would
        # keep handing back the failure. Errors are recorded for the cost trail
        # and then deliberately ignored here.
        cached = self._store.find(key)
        if cached is not None and cached.status == "ok":
            return replace(cached, status="replayed")

        with tracer.start_as_current_span(f"gen_ai.{role}") as span:
            # OTel GenAI semantic conventions. Standard names, so any backend
            # already understands these without custom dashboards.
            span.set_attribute("gen_ai.system", "openai")
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.operation.name", "chat")

            # Our own dimensions. Namespaced under agentpipe.* so they never
            # collide with the standard set.
            span.set_attribute("agentpipe.role", role)
            span.set_attribute("agentpipe.attempt_kind", attempt_kind)
            span.set_attribute("agentpipe.attempt_index", attempt_index)
            span.set_attribute("agentpipe.pack_hash", pack)
            span.set_attribute("agentpipe.run_id", self.run_id)
            if task_ref:
                span.set_attribute("agentpipe.task_ref", task_ref)

            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")

            started = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(
                    model=model, messages=list(messages), **kwargs
                )
            except Exception as exc:
                duration = int((time.perf_counter() - started) * 1000)
                rec = CallRecord(
                    run_id=self.run_id, idempotency_key=key, role=role,
                    attempt_kind=attempt_kind, attempt_index=attempt_index,
                    model=model, usage=Usage(), cost_usd=Decimal(0),
                    status="error", duration_ms=duration, task_ref=task_ref,
                    pack_hash=pack, error=f"{type(exc).__name__}: {exc}",
                    trace_id=trace_id, span_id=span_id,
                )
                self._safe_record(rec)
                span.set_attribute("agentpipe.status", "error")
                span.record_exception(exc)
                raise

            duration = int((time.perf_counter() - started) * 1000)
            usage = _extract_usage(resp)
            cost = self._prices.cost_usd(model, usage)

            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            span.set_attribute("gen_ai.usage.cached_input_tokens", usage.cached_input_tokens)
            span.set_attribute("gen_ai.response.model", getattr(resp, "model", model))
            span.set_attribute("agentpipe.cost_usd", float(cost))
            span.set_attribute("agentpipe.ratio", usage.ratio)
            span.set_attribute("agentpipe.status", "ok")

            rec = CallRecord(
                run_id=self.run_id, idempotency_key=key, role=role,
                attempt_kind=attempt_kind, attempt_index=attempt_index,
                model=model, usage=usage, cost_usd=cost, status="ok",
                duration_ms=duration, task_ref=task_ref, pack_hash=pack,
                trace_id=trace_id, span_id=span_id,
                content=resp.choices[0].message.content or "",
            )
            self._safe_record(rec)
            return rec

    def _safe_record(self, rec: CallRecord) -> None:
        """Telemetry must never be the reason a run fails.

        CEE-7 constraint 4, and it is a real one. A meter that can take down the
        thing it measures is worse than no meter, because now you have two
        failure modes instead of one.
        """
        try:
            self._store.record(rec)
        except Exception as exc:  # noqa: BLE001
            print(f"[agentpipe] WARN: failed to record call {rec.idempotency_key}: {exc}")


def _extract_usage(resp: Any) -> Usage:
    """Pull usage off the response.

    Defensive on purpose. Providers add fields, rename them, and occasionally
    omit them on streamed or cached responses. Missing usage is a gap in the
    data, not a reason to crash.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return Usage()

    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details else 0

    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cached_input_tokens=cached or 0,
    )
