"""v6.1 model usage and cost tracking, and a fallback model for transient provider failures."""

import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.intelligence.base import ProviderError, TransientProviderError
from jarvis.intelligence.openai_provider import FallbackProvider, OpenAIProvider
from jarvis.intelligence.router import build_provider
from jarvis.main import create_app
from jarvis.usage.service import MemoryUsageStore, Price, UsageService

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v61-usage-fallback-token-with-32-chars-x"


def adapter(transport, model="primary-model", role="primary"):
    return OpenAIProvider(api_key=SecretStr("test-key"), base_url="https://provider.example/v1", model=model,
                          connect_timeout_seconds=1, read_timeout_seconds=1, transport=transport, role=role)


def ok(content="Hello.", usage=True):
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    return httpx.Response(200, json=body)


def recorded(service):
    assert service.flush(5)
    return service.store.events()


def test_every_call_is_recorded_with_reported_tokens_and_no_text():
    service = UsageService(MemoryUsageStore())
    provider = adapter(httpx.MockTransport(lambda request: ok("Secret reply text.")))
    provider.usage_recorder = service.record
    assert asyncio.run(provider.generate_reply("Secret prompt text.")) == "Secret reply text."
    events = recorded(service)
    assert len(events) == 1
    event = events[0]
    assert (event.model, event.role, event.outcome, event.prompt_tokens, event.completion_tokens) == (
        "primary-model", "primary", "succeeded", 120, 30)
    assert "Secret" not in event.model_dump_json()


def test_failures_and_missing_token_counts_are_recorded_honestly():
    service = UsageService(MemoryUsageStore())
    failing = adapter(httpx.MockTransport(lambda request: httpx.Response(503)))
    failing.usage_recorder = service.record
    with pytest.raises(TransientProviderError):
        asyncio.run(failing.generate_reply("x"))

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    timing_out = adapter(httpx.MockTransport(slow))
    timing_out.usage_recorder = service.record
    with pytest.raises(TransientProviderError):
        asyncio.run(timing_out.generate_reply("x"))
    silent = adapter(httpx.MockTransport(lambda request: ok(usage=False)))
    silent.usage_recorder = service.record
    asyncio.run(silent.generate_reply("x"))
    outcomes = [(event.outcome, event.prompt_tokens) for event in recorded(service)]
    assert outcomes == [("failed", None), ("timeout", None), ("succeeded", None)]
    row = service.summary(1).by_model_and_day[0]
    # every call without full token counts is counted, failed ones too (they may still be billed)
    assert row.calls == 3 and row.failed_calls == 2 and row.calls_without_token_counts == 3


def test_a_broken_usage_store_never_breaks_the_reply():
    class Broken:
        durable = False

        def append(self, event):
            raise RuntimeError("database down")

        def since(self, start):
            return []

    service = UsageService(Broken())
    provider = adapter(httpx.MockTransport(lambda request: ok()))
    provider.usage_recorder = service.record
    assert asyncio.run(provider.generate_reply("x")) == "Hello."
    service.flush(5)
    assert service.recording_failures == 1


def test_cost_is_estimated_only_with_a_declared_price():
    unpriced = UsageService(MemoryUsageStore())
    unpriced.record(provider="openai", model="m", outcome="succeeded", duration_ms=5, prompt_tokens=1_000_000,
                    completion_tokens=500_000)
    summary = unpriced.summary(1)
    assert summary.totals.estimated_cost_usd is None
    assert summary.by_model_and_day[0].cost_note.startswith("unknown")
    priced = UsageService(MemoryUsageStore(), {"m": Price(0.5, 1.5, "provider pricing page, 2026-10-24")})
    priced.record(provider="openai", model="m", outcome="succeeded", duration_ms=5, prompt_tokens=1_000_000,
                  completion_tokens=500_000)
    summary = priced.summary(1)
    assert summary.totals.estimated_cost_usd == pytest.approx(0.5 + 0.75)
    assert summary.by_model_and_day[0].day == datetime.now(timezone.utc).date()


def test_fallback_answers_only_when_the_primary_is_temporarily_unavailable():
    service = UsageService(MemoryUsageStore())
    primary = adapter(httpx.MockTransport(lambda request: httpx.Response(503)))
    fallback = adapter(httpx.MockTransport(lambda request: ok("From the fallback.")), "fallback-model", "fallback")
    provider = FallbackProvider(primary, fallback)
    provider.usage_recorder = service.record
    assert asyncio.run(provider.generate_reply("x")) == "From the fallback."
    assert [(event.model, event.role, event.outcome) for event in recorded(service)] == [
        ("primary-model", "primary", "failed"), ("fallback-model", "fallback", "succeeded")]
    rejected = FallbackProvider(adapter(httpx.MockTransport(lambda request: httpx.Response(400))),
                                adapter(httpx.MockTransport(lambda request: ok()), "fallback-model", "fallback"))
    with pytest.raises(ProviderError):  # a rejected request is not sent again to another model
        asyncio.run(rejected.generate_reply("x"))


def test_the_fallback_model_is_configured_from_settings():
    settings = Settings(_env_file=None, provider="openai", openai_api_key=SecretStr("k"),
                        local_auth_token=SecretStr(TOKEN), owner_id=OWNER, openai_fallback_model="second-model")
    provider = build_provider("openai", settings=settings)
    assert isinstance(provider, FallbackProvider) and provider.fallback.model == "second-model"
    assert isinstance(build_provider("openai", settings=settings.model_copy(update={"openai_fallback_model": None})),
                      OpenAIProvider)


def test_usage_endpoint_and_status_through_the_app():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(503) if len(seen) == 1 else ok("Answer.")

    settings = Settings(_env_file=None, environment="test", log_level="ERROR", provider="openai",
                        openai_api_key=SecretStr("k"), local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    provider = FallbackProvider(adapter(httpx.MockTransport(handler)),
                                adapter(httpx.MockTransport(handler), "fallback-model", "fallback"))
    with TestClient(create_app(settings=settings, provider=provider),
                    headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        chat = client.post("/chat", json={"message": "hi"}, headers={"X-Request-ID": "trace-usage"})
        usage = client.get("/usage", params={"days": 1}).json()
        status = client.get("/status").json()
        assert client.get("/usage", headers={"Authorization": "Bearer wrong-token-value-of-32-characters"}).status_code == 401
    assert chat.status_code == 200
    assert usage["totals"]["calls"] >= 2 and any(row["fallback_calls"] for row in usage["by_model_and_day"])
    checks = next(item for item in status["subsystems"] if item["key"] == "provider")["checks"]
    assert "fallback model configured: fallback-model" in checks


# ------------------------------------------------------------- review fixes

import time  # noqa: E402

from jarvis.advanced_intelligence import build_intelligence  # noqa: E402
from jarvis.core.correlation import _current  # noqa: E402


class SlowStore(MemoryUsageStore):
    def append(self, event):
        time.sleep(2)
        super().append(event)


def test_a_slow_usage_store_never_delays_the_reply():
    service = UsageService(SlowStore())
    provider = adapter(httpx.MockTransport(lambda request: ok()))
    provider.usage_recorder = service.record
    started = time.monotonic()
    assert asyncio.run(provider.generate_reply("x")) == "Hello."
    assert time.monotonic() - started < 1.0


def test_a_deadline_cancellation_is_recorded_as_cancelled_and_returns_on_time():
    service = UsageService(SlowStore())

    async def hang(request):
        await asyncio.sleep(10)
        return ok()

    provider = adapter(httpx.MockTransport(hang))
    provider.usage_recorder = service.record

    async def call():
        await asyncio.wait_for(provider.generate_reply("x"), timeout=0.3)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(call())
    assert time.monotonic() - started < 1.5  # the slow store did not hold the deadline back
    assert service.flush(5) and [event.outcome for event in service.store.events()] == ["cancelled"]


def test_a_full_queue_drops_and_counts_instead_of_waiting():
    blocker = threading_event = __import__("threading").Event()

    class BlockedStore(MemoryUsageStore):
        def append(self, event):
            blocker.wait(5)

    service = UsageService(BlockedStore(), capacity=2)
    for _ in range(10):
        service.record(provider="openai", model="m", outcome="succeeded", duration_ms=1)
    assert service.dropped_events >= 7
    threading_event.set()


def test_a_failing_fallback_is_final_so_retries_do_not_multiply():
    calls = []

    def unavailable(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(503)

    provider = FallbackProvider(adapter(httpx.MockTransport(unavailable)),
                                adapter(httpx.MockTransport(unavailable), "fallback-model", "fallback"))
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.generate_reply("x"))
    assert not isinstance(raised.value, TransientProviderError) and calls == ["primary-model", "fallback-model"]


def test_router_and_evaluation_measure_the_registered_model_not_the_fallback():
    settings = Settings(_env_file=None, provider="openai", openai_api_key=SecretStr("k"),
                        local_auth_token=SecretStr(TOKEN), owner_id=OWNER, openai_fallback_model="second-model")
    provider = build_provider("openai", settings=settings)
    layer = build_intelligence(settings, provider)
    assert layer.router.adapters["openai"] is provider.primary and provider.model == settings.openai_model


@pytest.mark.parametrize("overrides, message", [
    ({"openai_fallback_model": "gpt-4o-mini"}, "must differ"),
    ({"openai_price_input_per_million": 1.0, "openai_price_source": "page"}, "both the input and the output"),
    ({"openai_price_input_per_million": 1.0, "openai_price_output_per_million": 2.0}, "PRICE_SOURCE is required"),
    ({"openai_fallback_price_input_per_million": 1.0, "openai_fallback_price_output_per_million": 1.0,
      "openai_price_source": "page"}, "needs JARVIS_OPENAI_FALLBACK_MODEL"),
])
def test_usage_settings_are_validated(overrides, message):
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **overrides)


def test_the_summary_covers_whole_utc_days_and_the_correlation_id_is_kept():
    service = UsageService(MemoryUsageStore())
    token = _current.set("trace-usage-1")
    try:
        service.record(provider="openai", model="m", outcome="succeeded", duration_ms=1, prompt_tokens=1)
    finally:
        _current.reset(token)
    summary = service.summary(1)
    assert summary.first_day == datetime.now(timezone.utc).date() and len(summary.by_model_and_day) == 1
    assert summary.by_model_and_day[0].calls_without_token_counts == 1  # completion count missing
    assert service.store.events()[0].correlation_id == "trace-usage-1"
    assert "may still be billed" in summary.note
