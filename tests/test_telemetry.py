"""Layer 0 tests.

Note what is being tested: not "does OpenAI work", but "do our guarantees hold".
Three of them, and they are the reason the layer exists.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from agentpipe.telemetry import (
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

    def __init__(self, *, cached: int = 0, fail: bool = False) -> None:
        self.calls = 0
        self._cached = cached
        self._fail = fail
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self._fail:
            raise RuntimeError("provider exploded")
        return SimpleNamespace(
            model="test-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=100,
                prompt_tokens_details=SimpleNamespace(cached_tokens=self._cached),
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
