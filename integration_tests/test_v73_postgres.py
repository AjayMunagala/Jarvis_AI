"""v7.3 live PostgreSQL validation: event sources CRUD, polling with a fake fetcher, durable dedup, cascade delete."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

FEED_URL = "https://feeds.example.com/news.xml"


def _rss(*guids):
    body = "".join(f"<item><title>Title {guid}</title><guid>{guid}</guid>"
                   f"<link>https://example.com/{guid}</link></item>" for guid in guids)
    return f'<rss version="2.0"><channel>{body}</channel></rss>'.encode("utf-8")


class _Fetcher:
    def __init__(self, document):
        self.document = document

    def __call__(self, url):
        return self.document


def _service(url, fetcher, scheduler=None):
    from jarvis.scheduler.service import SchedulerService
    from jarvis.scheduler.stores import PostgresSchedulerStore
    from jarvis.sources.service import SourcesService
    from jarvis.sources.stores import PostgresSourceStore

    scheduler = scheduler or SchedulerService(PostgresSchedulerStore(url))
    return SourcesService(PostgresSourceStore(url), scheduler_service=scheduler, feed_fetcher=fetcher)


def _request(**overrides):
    from jarvis.sources.models import SourceConfig, SourceCreateRequest

    return SourceCreateRequest(kind="rss", name="News", config=SourceConfig(url=FEED_URL), **overrides)


def test_sources_round_trip_poll_and_durable_dedup(isolated_postgres):
    from jarvis.sources.models import SourceUpdateRequest
    from jarvis.sources.service import SourceNotFound

    url, owner, other = isolated_postgres.database_url, uuid4(), uuid4()
    fetcher = _Fetcher(_rss("a", "b"))
    service = _service(url, fetcher)
    source = service.create_source(owner, _request(include_keywords=["title"], importance="high"))
    assert service.get_source(owner, source.id) == source
    with pytest.raises(SourceNotFound):
        service.get_source(other, source.id)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    first = service.poll_now(owner, source.id, now=now)
    assert (first.baseline, first.fetched, first.new, first.notified) == (True, 2, 0, 0)
    assert service.poll_now(owner, source.id, now=now + timedelta(minutes=1)).new == 0

    # a new service instance (as after a restart) still knows what was seen
    fetcher.document = _rss("c", "a", "b")
    restarted = _service(url, fetcher)
    result = restarted.poll_now(owner, source.id, now=now + timedelta(minutes=2))
    assert (result.baseline, result.new, result.notified) == (False, 1, 1)
    notes, _ = restarted.scheduler_service.store.list_notifications(owner, limit=10)
    assert [note.title for note in notes] == ["News: Title c"] and notes[0].category == "source"
    items = restarted.list_items(owner, source.id).items
    assert [item.title for item in items][0] == "Title c" and len(items) == 3
    stored = restarted.get_source(owner, source.id)
    assert stored.last_success_at == now + timedelta(minutes=2) and stored.consecutive_failures == 0

    updated = restarted.update_source(owner, source.id, SourceUpdateRequest(enabled=False, exclude_keywords=["x"]))
    assert restarted.get_source(owner, source.id) == updated and updated.exclude_keywords == ["x"]
    assert restarted.list_sources(other).total == 0 and restarted.list_sources(owner).total == 1


def test_failures_are_recorded_and_the_source_is_turned_off(isolated_postgres):
    from jarvis.sources.fetchers import FetchError
    from jarvis.sources.service import AUTO_DISABLE_AFTER_FAILURES

    def failing(url):
        raise FetchError("http_error", "The feed answered HTTP 503.")

    url, owner = isolated_postgres.database_url, uuid4()
    service = _service(url, failing)
    source = service.create_source(owner, _request())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for attempt in range(AUTO_DISABLE_AFTER_FAILURES):
        result = service.poll_now(owner, source.id, now=now + timedelta(days=attempt))
    stored = service.get_source(owner, source.id)
    assert result.disabled and not stored.enabled and stored.consecutive_failures == AUTO_DISABLE_AFTER_FAILURES
    assert stored.last_error == "The feed answered HTTP 503." and stored.backoff_until is not None
    notes, _ = service.scheduler_service.store.list_notifications(owner, limit=10)
    assert [note.title for note in notes] == ["Source turned off: News"]


def test_the_cap_and_cascade_delete(isolated_postgres):
    import psycopg

    from jarvis.sources.service import SourceConflict

    url, owner = isolated_postgres.database_url, uuid4()
    service = _service(url, _Fetcher(_rss(*[str(n) for n in range(30)])))
    source = service.create_source(owner, _request())
    service.poll_now(owner, source.id, now=datetime.now(timezone.utc))
    with psycopg.connect(url) as conn:
        count = conn.execute("SELECT count(*) FROM jarvis_app.event_source_items WHERE source_id = %s",
                             (source.id,)).fetchone()[0]
    assert count == 30
    for n in range(49):
        service.create_source(owner, _request())
    with pytest.raises(SourceConflict):
        service.create_source(owner, _request())
    service.delete_source(owner, source.id)
    with psycopg.connect(url) as conn:
        remaining = conn.execute("SELECT count(*) FROM jarvis_app.event_source_items WHERE source_id = %s",
                                 (source.id,)).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("""INSERT INTO jarvis_app.event_sources (id, owner_id, kind, name, url, poll_interval_minutes,
                                created_at, updated_at) VALUES (%s, %s, 'rss', 'x', 'https://a.example', 5, now(), now())""",
                         (uuid4(), owner))
    assert remaining == 0


def test_seen_items_are_pruned_to_the_newest_thousand(isolated_postgres):
    url, owner = isolated_postgres.database_url, uuid4()
    fetcher = _Fetcher(b"")
    service = _service(url, fetcher)
    source = service.create_source(owner, _request())
    start = datetime.now(timezone.utc).replace(microsecond=0)
    for batch in range(6):
        fetcher.document = _rss(*[f"{batch}-{n}" for n in range(200)])
        service.poll_now(owner, source.id, now=start + timedelta(hours=batch))
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT title FROM jarvis_app.event_source_items WHERE source_id = %s",
                            (source.id,)).fetchall()
    titles = {row[0] for row in rows}
    assert len(titles) == 1000 and all(f"Title 5-{n}" in titles for n in range(200))
    assert not any(title.startswith("Title 0-") for title in titles)


def test_two_store_instances_count_failures_atomically_and_turn_off_once(isolated_postgres):
    from concurrent.futures import ThreadPoolExecutor

    from jarvis.sources.models import SourceUpdateRequest
    from jarvis.sources.stores import PostgresSourceStore

    url, owner = isolated_postgres.database_url, uuid4()
    service = _service(url, _Fetcher(_rss("a")))
    source = service.create_source(owner, _request())
    stores = [PostgresSourceStore(url), PostgresSourceStore(url)]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(
            lambda n: stores[n % 2].record_failure(owner, source.id, now=now, error="HTTP 500.", disable_after=10),
            range(12)))
    assert sorted(outcome.consecutive_failures for outcome in outcomes) == list(range(1, 13))
    assert sum(outcome.turned_off for outcome in outcomes) == 1  # exactly one update turned it off
    stored = stores[0].get_source(owner, source.id)
    assert not stored.enabled and stored.consecutive_failures == 12
    assert stored.backoff_until == now + timedelta(hours=24)
    # an edit made from a stale copy never writes back poll state; re-enabling resets it
    stale = stored.model_copy(update={"consecutive_failures": 0, "backoff_until": None})
    renamed = stores[1].update_settings(stale.model_copy(update={"name": "Renamed"}), reset_failures=False)
    assert renamed.name == "Renamed" and renamed.consecutive_failures == 12 and renamed.backoff_until is not None
    reenabled = service.update_source(owner, source.id, SourceUpdateRequest(enabled=True))
    assert reenabled.enabled and reenabled.consecutive_failures == 0 and reenabled.backoff_until is None


def test_folder_versions_and_per_source_throttle_survive_a_restart(isolated_postgres, tmp_path):
    import os

    from jarvis.sources.models import SourceConfig, SourceCreateRequest
    from jarvis.sources.service import SourcesService
    from jarvis.sources.stores import PostgresSourceStore
    from jarvis.scheduler.service import SchedulerService
    from jarvis.scheduler.stores import PostgresSchedulerStore

    url, owner = isolated_postgres.database_url, uuid4()

    def fresh():
        return SourcesService(PostgresSourceStore(url), scheduler_service=SchedulerService(PostgresSchedulerStore(url)),
                              folder_roots=(tmp_path,))

    (tmp_path / "a.txt").write_text("a")
    source = fresh().create_source(owner, SourceCreateRequest(kind="folder", name="Inbox",
                                                              config=SourceConfig(path=str(tmp_path)),
                                                              poll_interval_minutes=15))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert fresh().poll_now(owner, source.id, now=now).baseline
    (tmp_path / "a.txt").write_text("changed")
    os.utime(tmp_path / "a.txt", ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    assert fresh().poll_now(owner, source.id, now=now + timedelta(minutes=15)).pending == 1
    changed = fresh().poll_now(owner, source.id, now=now + timedelta(minutes=30))
    assert (changed.changed, changed.notified) == (1, 1)
    (tmp_path / "b.txt").write_text("b")
    fresh().poll_now(owner, source.id, now=now + timedelta(minutes=35))
    throttled = fresh().poll_now(owner, source.id, now=now + timedelta(minutes=40))
    assert (throttled.new, throttled.suppressed) == (1, 1)  # the per-source throttle read from storage
    item = next(item for item in fresh().list_items(owner, source.id).items if item.title == "a.txt")
    assert item.version == item.settled_version and item.notified_at == now + timedelta(minutes=30)
