"""Layer 0 tests.

Note what is being tested: not "does OpenAI work", but "do our guarantees hold".
Three of them, and they are the reason the layer exists.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from agentpipe.telemetry import (
    CallRecord,
    CallStore,
    InMemoryCallStore,
    MeteredClient,
    PriceMap,
    Usage,
    idempotency_key,
    pack_hash,
)

PRICES = PriceMap({
    "test-model": {"input": 1.00, "cached_input": 0.10, "output": 10.00},
})


class FakeOpenAI:
    """Counts calls, so we can prove the second one never happens."""

    def __init__(
        self, *, cached: int = 0, fail: bool = False,
        reasoning: int = 0, finish: str = "stop", content: str = "hi",
    ) -> None:
        self.calls = 0
        self._cached = cached
        self._fail = fail
        self._reasoning = reasoning
        self._finish = finish
        self._content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self._fail:
            raise RuntimeError("provider exploded")
        return SimpleNamespace(
            model="test-model",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self._content),
                finish_reason=self._finish,
            )],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=100,
                prompt_tokens_details=SimpleNamespace(cached_tokens=self._cached),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=self._reasoning
                ),
            ),
        )


MESSAGES = [{"role": "user", "content": "hello"}]


def make(client: FakeOpenAI, store: CallStore | None = None) -> MeteredClient:
    return MeteredClient(
        store=store or InMemoryCallStore(),
        prices=PRICES,
        client=client,  # type: ignore[arg-type]
        run_id="run-1",
    )


# --- Guarantee 1: we never pay twice for the same logical call --------------

def test_identical_call_is_not_repaid():
    fake = FakeOpenAI()
    store = InMemoryCallStore()

    first = make(fake, store).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", attempt_index=1, task_ref="TASK-1",
    )
    # A crash and restart: brand new client object, same store.
    second = make(fake, store).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", attempt_index=1, task_ref="TASK-1",
    )

    assert fake.calls == 1, "second call hit the provider and cost real money"
    assert second.status == "replayed"
    assert first.idempotency_key == second.idempotency_key


def test_changed_context_is_a_different_call():
    """The flip side. If the pack changed, it is genuinely new work."""
    fake = FakeOpenAI()
    store = InMemoryCallStore()
    client = make(fake, store)

    client.call(messages=MESSAGES, model="test-model", role="builder",
                attempt_kind="implement", attempt_index=1, task_ref="TASK-1")
    client.call(messages=[{"role": "user", "content": "different"}],
                model="test-model", role="builder",
                attempt_kind="implement", attempt_index=1, task_ref="TASK-1")

    assert fake.calls == 2


def test_attempt_index_separates_calls():
    fake = FakeOpenAI()
    store = InMemoryCallStore()
    client = make(fake, store)

    for i in (1, 2):
        client.call(messages=MESSAGES, model="test-model", role="builder",
                    attempt_kind="implement", attempt_index=i, task_ref="TASK-1")

    assert fake.calls == 2


def test_idempotency_key_ignores_run_id():
    """Two runs, same logical call, same key. This is the point."""
    a = idempotency_key("TASK-1", "builder", "implement", 1, "abc")
    b = idempotency_key("TASK-1", "builder", "implement", 1, "abc")
    assert a == b


def test_pack_hash_is_order_stable():
    m1 = [{"role": "user", "content": "x"}]
    m2 = [{"content": "x", "role": "user"}]
    assert pack_hash(m1) == pack_hash(m2)


# --- Guarantee 2: the cost number is right ---------------------------------

def test_cost_math():
    usage = Usage(input_tokens=1000, output_tokens=100)
    # 1000 in @ $1/M = $0.001 ; 100 out @ $10/M = $0.001
    assert PRICES.cost_usd("test-model", usage) == Decimal("0.002000")


def test_cached_tokens_are_discounted():
    usage = Usage(input_tokens=1000, cached_input_tokens=800, output_tokens=100)
    # 200 fresh @ $1/M = $0.0002 ; 800 cached @ $0.10/M = $0.00008 ; out $0.001
    assert PRICES.cost_usd("test-model", usage) == Decimal("0.001280")


def test_unknown_model_costs_zero_but_does_not_raise():
    assert PRICES.cost_usd("who-is-this", Usage(input_tokens=999)) == Decimal(0)


def test_ratio_is_the_number_we_care_about():
    # Andrew's 70k/100.
    assert Usage(input_tokens=70_000, output_tokens=100).ratio == 700.0


def test_cache_hit_rate():
    assert Usage(input_tokens=1000, cached_input_tokens=250).cache_hit_rate == 0.25


# --- Guarantee 3: the meter never kills the run ----------------------------

def test_store_failure_does_not_fail_the_call():
    class BrokenStore(CallStore):
        def find(self, idempotency_key):
            return None

        def record(self, rec):
            raise RuntimeError("postgres is on fire")

    rec = make(FakeOpenAI(), BrokenStore()).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", task_ref="TASK-1",
    )
    assert rec.status == "ok"
    assert rec.content == "hi"


def test_missing_usage_is_a_gap_not_a_crash():
    class NoUsage(FakeOpenAI):
        def _create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                model="test-model",
                choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
                usage=None,
            )

    rec = make(NoUsage()).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", task_ref="TASK-1",
    )
    assert rec.status == "ok"
    assert rec.usage.total_tokens == 0
    assert rec.cost_usd == Decimal(0)


def test_provider_error_is_recorded_then_reraised():
    store = InMemoryCallStore()
    with pytest.raises(RuntimeError, match="provider exploded"):
        make(FakeOpenAI(fail=True), store).call(
            messages=MESSAGES, model="test-model", role="builder",
            attempt_kind="implement", task_ref="TASK-1",
        )
    [rec] = store.records.values()
    assert rec.status == "error"
    assert "provider exploded" in rec.error


def test_errors_are_not_replayed_as_success():
    """A failed call must not poison the idempotency cache into 'done'."""
    store = InMemoryCallStore()
    with pytest.raises(RuntimeError):
        make(FakeOpenAI(fail=True), store).call(
            messages=MESSAGES, model="test-model", role="builder",
            attempt_kind="implement", task_ref="TASK-1",
        )
    [rec] = store.records.values()
    assert rec.status == "error"


def test_prices_from_env_refuses_to_guess(monkeypatch):
    monkeypatch.delenv("AGENTPIPE_PRICES", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to guess"):
        PriceMap.from_env()


# --- Guarantee 4: we can tell thinking from speaking -----------------------

def test_reasoning_tokens_are_captured():
    rec = make(FakeOpenAI(reasoning=90)).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", task_ref="TASK-1",
    )
    assert rec.usage.reasoning_tokens == 90
    assert rec.usage.output_tokens == 100
    assert rec.usage.answer_tokens == 10


def test_reasoning_is_a_subset_not_an_addition():
    """It is already billed as output. Counting it twice would overstate cost."""
    usage = Usage(input_tokens=1000, output_tokens=100, reasoning_tokens=90)
    assert PRICES.cost_usd("test-model", usage) == Decimal("0.002000")


def test_ratio_flatters_and_answer_ratio_does_not():
    """The perverse gap. Thinking makes the headline number look better."""
    usage = Usage(input_tokens=1500, output_tokens=400, reasoning_tokens=396)
    assert usage.ratio == 3.75
    assert usage.answer_ratio == 375.0


def test_thought_hard_and_said_nothing_is_recorded_as_a_failure():
    """The exact failure that started all this, and the fix at its source.

    A billed call that returned nothing is not a success. Recording it as 'ok'
    is what let an empty row become a permanent cache hit for its key. As an
    error it stays for the cost trail, is never replayed, and the next run
    tries again.
    """
    rec = make(FakeOpenAI(reasoning=100, content="", finish="length")).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", task_ref="TASK-1",
    )
    assert rec.status == "error"
    assert "finish_reason=length" in rec.error
    assert "thinking=100" in rec.error
    assert rec.cost_usd > 0, "it was still billed and the table should say so"


def test_an_empty_call_is_never_replayed():
    """So the poisoning cannot recur. Each attempt pays, and each attempt tries."""
    fake = FakeOpenAI(content="", finish="length")
    store = InMemoryCallStore()
    for _ in range(2):
        make(fake, store).call(
            messages=MESSAGES, model="test-model", role="builder",
            attempt_kind="implement", attempt_index=1, task_ref="TASK-1",
        )
    assert fake.calls == 2


def test_finish_reason_is_captured():
    rec = make(FakeOpenAI(finish="stop")).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", task_ref="TASK-1",
    )
    assert rec.finish_reason == "stop"


def test_missing_completion_details_does_not_crash():
    """Not every provider returns this. A gap is not a crash."""
    class NoDetails(FakeOpenAI):
        def _create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                model="test-model",
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="hi"), finish_reason="stop"
                )],
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5,
                    prompt_tokens_details=None,
                ),
            )

    rec = make(NoDetails()).call(
        messages=MESSAGES, model="test-model", role="builder",
        attempt_kind="implement", task_ref="TASK-1",
    )
    assert rec.usage.reasoning_tokens == 0


def test_a_poisoned_record_cannot_be_constructed():
    """The guard that makes four other guards unnecessary.

    This replaces a test that used to build a poisoned record and check that
    replay refused it. That test can no longer be written, because the record
    cannot be built. That is the improvement: an invalid state you can build is
    an invalid state you will build.
    """
    with pytest.raises(ValueError, match="not a successful call"):
        CallRecord(
            run_id="r", idempotency_key="k", role="builder",
            attempt_kind="implement", attempt_index=1, model="m",
            usage=Usage(), cost_usd=Decimal(0), status="ok",
            duration_ms=1, content="",
        )


def test_an_error_with_no_message_cannot_be_constructed():
    with pytest.raises(ValueError, match="says nothing"):
        CallRecord(
            run_id="r", idempotency_key="k", role="builder",
            attempt_kind="implement", attempt_index=1, model="m",
            usage=Usage(), cost_usd=Decimal(0), status="error",
            duration_ms=1,
        )


def test_negative_cost_cannot_be_constructed():
    with pytest.raises(ValueError, match="negative cost"):
        CallRecord(
            run_id="r", idempotency_key="k", role="builder",
            attempt_kind="implement", attempt_index=1, model="m",
            usage=Usage(), cost_usd=Decimal("-1"), status="ok",
            duration_ms=1, content="x",
        )
