"""v6.1 live PostgreSQL validation: usage events are durable and queryable after a restart."""


def test_usage_events_survive_a_restart(isolated_postgres):
    from jarvis.usage.service import PostgresUsageStore, UsageService

    url = isolated_postgres.database_url
    first = UsageService(PostgresUsageStore(url))
    first.record(provider="openai", model="live-model", outcome="succeeded", duration_ms=12.5, prompt_tokens=10,
                 completion_tokens=5)
    first.record(provider="openai", model="live-model", outcome="timeout", duration_ms=1000, role="fallback")
    assert first.recording_failures == 0
    restarted = UsageService(PostgresUsageStore(url))
    row = next(item for item in restarted.summary(1).by_model_and_day if item.model == "live-model")
    assert (row.calls, row.failed_calls, row.fallback_calls, row.prompt_tokens, row.completion_tokens) == (2, 1, 1, 10, 5)
    assert restarted.summary(1).durable_storage is True
