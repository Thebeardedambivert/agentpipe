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


def configure_tracing(console: bool | None = None) -> None:
    """Turn the no-op tracer into a real one, so trace_id and span_id stop being
    zeros.

    Without this, the OTel API's default provider throws every span away and the
    seam records all-zero ids: the span columns look official and hold nothing.
    With it, ids are real and a run's calls share a trace_id, so the ledger's
    trace columns become queryable, and a run reads as a tree rather than a list.

    Opt-in, called from the CLI entry rather than on import, so library and test
    use stay no-op and nothing sets global OTel state behind a caller's back.
    Idempotent: a second call is a no-op, which keeps OTel from warning about a
    provider being overridden.

    Spans export to the console only when asked (`console=True` or
    AGENTPIPE_TRACE_CONSOLE); otherwise they are recorded with real ids and not
    shipped anywhere, which is all the database ledger needs. A real backend
    (an OTLP collector) is a one-processor change here, once there is a viewer
    worth pointing it at.
    """
    from opentelemetry.sdk.trace import TracerProvider

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return  # already configured

    provider = TracerProvider()
    if console or (console is None and os.environ.get("AGENTPIPE_TRACE_CONSOLE")):
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


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
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def answer_tokens(self) -> int:
        """Output that was actually the reply.

        Reasoning tokens are billed as output but are not part of the answer.
        A response can be 400 output tokens and 0 answer tokens: the model
        thought hard and said nothing. That is indistinguishable from "the model
        had nothing to say" unless the two are counted separately, which is why
        this field exists.
        """
        return max(0, self.output_tokens - self.reasoning_tokens)

    @property
    def ratio(self) -> float:
        """Input per unit of output. Andrew's 70k/100 is a ratio of 700.

        Billed view. Uses output_tokens, so reasoning counts.
        """
        if self.output_tokens == 0:
            return float("inf")
        return self.input_tokens / self.output_tokens

    @property
    def answer_ratio(self) -> float:
        """Input per unit of actual answer.

        Read this next to ratio, because reasoning creates a perverse gap
        between them. Turn reasoning effort up and ratio improves while the bill
        grows: 1,500 in and 400 out reads as a healthy 3.8, even when 396 of
        those tokens were thinking and the reply was four words. answer_ratio
        says 375 and is telling the truth.
        """
        if self.answer_tokens == 0:
            return float("inf")
        return self.input_tokens / self.answer_tokens

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
    finish_reason: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    content: str = ""

    def __post_init__(self) -> None:
        """Refuse to exist in a state that lies.

        This is the cheap version of a lesson that cost an afternoon. A record
        marked 'ok' with no content was constructible, so it got constructed,
        stored, read back, and replayed. Four places could each have caught it.
        None did, because none of them was *the* place.

        Checking at every use site is how you end up with a codebase made of
        paranoia and still miss the fifth site. Checking at construction is one
        place, and it catches every path in and out, including ones nobody has
        written yet.

        The general shape: an invalid state you can build is an invalid state
        you will build. Prefer making it unrepresentable to guarding against it.
        """
        if self.status == "ok" and not self.content and not self.error:
            raise ValueError(
                "a successful call with no content is not a successful call. "
                "Either the reply was genuinely empty, in which case say so "
                "with status='error', or something dropped it on the way in."
            )
        if self.status == "error" and not self.error:
            raise ValueError("status='error' with no error message says nothing")
        if self.cost_usd < 0:
            raise ValueError(f"negative cost: {self.cost_usd}")


# ---------------------------------------------------------------------------
# Store (a port, in A1's sense: swap Postgres for memory without touching
# the client)
# ---------------------------------------------------------------------------

class CallStore(ABC):
    """The contract every store must honour.

    Stated explicitly because the first version left it implicit, and the two
    implementations quietly disagreed. InMemoryCallStore kept the whole record
    including content. PostgresCallStore dropped content on the floor. Fifteen
    tests passed against the in-memory one while production replayed empty
    strings into a parser that then failed.

    The rule: find(record(x).idempotency_key) must return something equal to x
    in every field a caller can observe. Any store that cannot promise that is
    not a store, and tests/test_store_contract.py is where that promise is
    checked against all of them.
    """

    @abstractmethod
    def find(self, idempotency_key: str) -> CallRecord | None:
        """Return a previously recorded call, or None.

        Must round-trip content. A replayed call whose content is empty is
        worse than no cache at all: it turns a saving into a silent failure.
        """

    @abstractmethod
    def record(self, rec: CallRecord) -> None:
        """Append a call. Must be atomic on idempotency_key."""

    @abstractmethod
    def latest_for_run(self, run_id: str) -> CallRecord | None:
        """The highest-attempt call recorded for this run, or None.

        This is what makes a crashed run resumable. Its attempt_index says where
        to continue, and its stored content lets us re-apply work that was
        recorded but may never have reached disk, without paying for it again.
        """


class InMemoryCallStore(CallStore):
    """For tests, and for the first hour of Layer 0 before you wire Supabase."""

    def __init__(self) -> None:
        self.records: dict[str, CallRecord] = {}

    def find(self, idempotency_key: str) -> CallRecord | None:
        return self.records.get(idempotency_key)

    def record(self, rec: CallRecord) -> None:
        self.records.setdefault(rec.idempotency_key, rec)

    def latest_for_run(self, run_id: str) -> CallRecord | None:
        matches = [r for r in self.records.values() if r.run_id == run_id]
        return max(matches, key=lambda r: r.attempt_index, default=None)


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

    # One column list, one row-to-record mapping, used by every read. find and
    # latest_for_run returning different shapes of the same row is exactly the
    # store-divergence bug this project was burned by, so they share this.
    _COLS = (
        "run_id, idempotency_key, role, attempt_kind, attempt_index, model, "
        "input_tokens, cached_input_tokens, output_tokens, cost_usd, status, "
        "duration_ms, task_ref, pack_hash, error, reasoning_tokens, "
        "finish_reason, content"
    )

    @staticmethod
    def _to_record(row) -> CallRecord:
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
                reasoning_tokens=row[15] or 0,
            ),
            cost_usd=Decimal(row[9]),
            status=row[10],
            duration_ms=row[11],
            task_ref=row[12],
            pack_hash=row[13],
            error=row[14],
            finish_reason=row[16],
            content=row[17] or "",
        )

    def find(self, idempotency_key: str) -> CallRecord | None:
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                f"select {self._COLS} from model_calls where idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        return self._to_record(row) if row is not None else None

    def latest_for_run(self, run_id: str) -> CallRecord | None:
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                f"select {self._COLS} from model_calls "
                "where run_id = %s order by attempt_index desc limit 1",
                (run_id,),
            ).fetchone()
        return self._to_record(row) if row is not None else None

    def record(self, rec: CallRecord) -> None:
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(
                """
                insert into model_calls (
                    run_id, idempotency_key, role, attempt_kind, attempt_index,
                    task_ref, model, pack_hash, input_tokens, cached_input_tokens,
                    output_tokens, reasoning_tokens, cost_usd, status, error,
                    finish_reason, duration_ms, trace_id, span_id, content
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                on conflict (idempotency_key) do nothing
                """,
                (
                    rec.run_id, rec.idempotency_key, rec.role, rec.attempt_kind,
                    rec.attempt_index, rec.task_ref, rec.model, rec.pack_hash,
                    rec.usage.input_tokens, rec.usage.cached_input_tokens,
                    rec.usage.output_tokens, rec.usage.reasoning_tokens,
                    rec.cost_usd, rec.status, rec.error, rec.finish_reason,
                    rec.duration_ms, rec.trace_id, rec.span_id, rec.content,
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
    model: str,
) -> str:
    """Identity of a logical call.

    The key must contain everything that determines the output, because a replay
    hands back a past output under this key and calls it the answer. Anything that
    changes the answer but is missing from the key makes two different calls
    collide, and the second silently gets the first's reply.

    Deliberately excludes run_id. Two runs of the same task, at the same attempt
    index, with a byte-identical pack, on the same model, are the same call, and
    the second one should cost nothing. That is the whole point.

    Includes pack, so a changed context is genuinely new work and is paid for.

    Includes model. This was missing until Layer 5 Stage 2 made model routing a
    first-class, per-role choice: routing the fixer nano->mini, then re-running an
    identical pack, replayed nano's reply instead of calling mini, because the
    model was not in the key. A different model is a different computation at a
    different price, so it is a different call. Excluding it was a latent
    silent-wrong-answer bug that only became reachable once the model could vary
    while the pack stayed fixed. See test_a_model_change_is_not_replayed.

    Known remaining gap, stated rather than hidden: request parameters that also
    shape the output (temperature, reasoning effort, max_completion_tokens) are
    still not in the key. They are effectively constant in this project today, so
    the risk is small, but it is the same class of bug as the model one was. When
    any of them becomes a variable a run sweeps over, it belongs in here too.
    """
    raw = f"{task_ref}|{role}|{attempt_kind}|{attempt_index}|{model}|{pack}"
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
            task_ref or "adhoc", role, attempt_kind, attempt_index, pack, model
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
        #
        # The content check is belt and braces. CallRecord.__post_init__ makes
        # an 'ok' record with no content unconstructible, so a fresh row cannot
        # be poisoned. This guards the rows already in the table from before
        # that invariant existed, which is a real category: an invariant added
        # today says nothing about data written yesterday.
        cached = self._store.find(key)
        if cached is not None and cached.status == "ok" and cached.content:
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

            finish = _finish_reason(resp)

            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            span.set_attribute("gen_ai.usage.cached_input_tokens", usage.cached_input_tokens)
            span.set_attribute("gen_ai.usage.reasoning_tokens", usage.reasoning_tokens)
            span.set_attribute("gen_ai.response.model", getattr(resp, "model", model))
            span.set_attribute("gen_ai.response.finish_reasons", [finish or "unknown"])
            span.set_attribute("agentpipe.cost_usd", float(cost))
            span.set_attribute("agentpipe.ratio", usage.ratio)
            span.set_attribute("agentpipe.answer_tokens", usage.answer_tokens)
            span.set_attribute("agentpipe.status", "ok")

            content = resp.choices[0].message.content or ""

            # A billed call that returned nothing is a failure, not a success.
            # This is the fix for the poisoning at its source: recording it as
            # 'ok' is what let an empty row become a permanent cache hit. As an
            # error it stays in the table for the cost trail, is never replayed,
            # and the next run tries again.
            #
            # finish_reason usually says why. 'length' means the output budget
            # ran out, and on a reasoning model that means thinking ate the
            # whole allowance before it could speak.
            if not content:
                rec = CallRecord(
                    run_id=self.run_id, idempotency_key=key, role=role,
                    attempt_kind=attempt_kind, attempt_index=attempt_index,
                    model=model, usage=usage, cost_usd=cost, status="error",
                    duration_ms=duration, task_ref=task_ref, pack_hash=pack,
                    finish_reason=finish, trace_id=trace_id, span_id=span_id,
                    error=(
                        f"model returned no content "
                        f"(finish_reason={finish}, "
                        f"output={usage.output_tokens}, "
                        f"thinking={usage.reasoning_tokens})"
                    ),
                )
                span.set_attribute("agentpipe.status", "empty")
                self._safe_record(rec)
                return rec

            rec = CallRecord(
                run_id=self.run_id, idempotency_key=key, role=role,
                attempt_kind=attempt_kind, attempt_index=attempt_index,
                model=model, usage=usage, cost_usd=cost, status="ok",
                duration_ms=duration, task_ref=task_ref, pack_hash=pack,
                finish_reason=finish, trace_id=trace_id, span_id=span_id,
                content=content,
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

    def latest_attempt(self) -> CallRecord | None:
        """The most recent attempt recorded for this run, or None.

        For resuming a crashed run: the caller builds this client with the run_id
        it wants to continue, and this says where that run got to. Store access
        stays behind the one door.
        """
        return self._store.latest_for_run(self.run_id)


def _extract_usage(resp: Any) -> Usage:
    """Pull usage off the response.

    Defensive on purpose. Providers add fields, rename them, and occasionally
    omit them on streamed or cached responses. Missing usage is a gap in the
    data, not a reason to crash.

    Note that reasoning tokens are a *subset* of completion_tokens, not an
    addition to them. They are already billed as output. Counting them
    separately is about visibility, not cost: without it, "thought hard and said
    nothing" and "had nothing to say" produce identical rows.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return Usage()

    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(prompt_details, "cached_tokens", 0) if prompt_details else 0

    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning = (
        getattr(completion_details, "reasoning_tokens", 0)
        if completion_details else 0
    )

    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cached_input_tokens=cached or 0,
        reasoning_tokens=reasoning or 0,
    )


def _finish_reason(resp: Any) -> str | None:
    """Why the response ended.

    'stop' means the model finished. 'length' means it was cut off mid-thought
    and whatever you got is a fragment. 'content_filter' means something else
    entirely. One field, and it turns "the reply was empty" from a mystery into
    a fact.
    """
    try:
        return resp.choices[0].finish_reason
    except (AttributeError, IndexError):
        return None
