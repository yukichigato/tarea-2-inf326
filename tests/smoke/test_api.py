import asyncio
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from litestar.middleware import rate_limit
from litestar.stores.memory import MemoryStore
from litestar.testing import TestClient

from shortener.app import create_app, purge_expired_periodically
from shortener.domain import (
    INITIAL_ID,
    MAX_URL_LENGTH,
    AnalyticsUnavailable,
    HitLogger,
    encode_base62,
)
from shortener.settings import Settings
from shortener.storage import SQLiteURLRepository

pytestmark = pytest.mark.smoke


@pytest.fixture(params=["v1", "v2"])
def api(request: pytest.FixtureRequest, tmp_path: Path):
    settings = Settings(
        architecture=request.param,
        database_path=tmp_path / "urls.sqlite3",
        public_base_url="https://testserver.local",
        rate_limit_requests=100,
    )
    logger = Mock(spec=HitLogger)
    with TestClient(
        create_app(settings, hit_logger=logger), base_url="https://testserver.local"
    ) as client:
        yield client, settings, logger


def test_health_and_create_redirect_flow(api) -> None:
    client, settings, logger = api
    assert client.get("/health").json() == {"status": "ok", "architecture": settings.architecture}

    created = client.post("/urls", json={"long_url": "https://usm.cl/informatica/"})
    assert created.status_code == 201
    payload = created.json()
    assert payload["id"] == INITIAL_ID
    assert payload["short_url"] == f"https://testserver.local/{payload['hash']}"

    for _ in range(2):
        response = client.get(payload["short_url"], follow_redirects=False)
        assert response.status_code == (302 if settings.architecture == "v1" else 301)
        assert response.headers["location"] == "https://usm.cl/informatica/"
    if settings.architecture == "v1":
        assert response.headers["cache-control"] == "no-store"
        assert logger.record.call_count == 2
    else:
        assert response.headers["cache-control"] == "public, max-age=60"
        logger.record.assert_not_called()


@pytest.mark.parametrize(
    "payload", [{}, {"long_url": "not-a-url"}, {"long_url": "ftp://usm.cl"}, {"long_url": None}]
)
def test_invalid_urls_return_client_errors(api, payload) -> None:
    client, _, logger = api

    assert client.post("/urls", json=payload).status_code == 400
    logger.record.assert_not_called()


def test_missing_hash_returns_404_without_logging(api) -> None:
    client, _, logger = api

    assert client.get("/missing", follow_redirects=False).status_code == 404
    logger.record.assert_not_called()


def test_post_requests_always_create_a_new_record(api) -> None:
    client, _, _ = api
    payload = {"long_url": "https://usm.cl/"}

    first = client.post("/urls", json=payload).json()
    second = client.post("/urls", json=payload).json()

    assert second["id"] == first["id"] + 1
    assert second["hash"] != first["hash"]


def test_url_contract_preserves_the_normalized_destination_and_its_query(api) -> None:
    client, settings, logger = api
    original = "HTTPS://USM.CL/informatica/?year=2026&kind=postgrado"
    expected = "https://usm.cl/informatica/?year=2026&kind=postgrado"

    response = client.post("/urls", json={"long_url": original})
    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"id", "hash", "long_url", "short_url"}
    assert payload["long_url"] == expected
    redirect = client.get(f"/{payload['hash']}", follow_redirects=False)
    assert redirect.headers["location"] == expected
    if settings.architecture == "v1":
        hit = logger.record.call_args.args[0]
        assert hit.hash == payload["hash"]
        assert hit.url == expected
        assert hit.visited_at.utcoffset().total_seconds() == 0
    else:
        logger.record.assert_not_called()


def test_v1_events_tell_apart_short_urls_with_the_same_destination(tmp_path: Path) -> None:
    logger = Mock(spec=HitLogger)
    settings = Settings(architecture="v1", database_path=tmp_path / "urls.sqlite3")
    with TestClient(create_app(settings, hit_logger=logger)) as client:
        links = [
            client.post("/urls", json={"long_url": "https://usm.cl/admision"}).json()["hash"]
            for _ in range(2)
        ]
        for code in links:
            assert client.get(f"/{code}", follow_redirects=False).status_code == 302

    hits = [call.args[0] for call in logger.record.call_args_list]
    assert [hit.hash for hit in hits] == links
    assert {hit.url for hit in hits} == {"https://usm.cl/admision"}


def test_url_that_grows_past_the_limit_when_normalized_is_rejected(api) -> None:
    client, _, _ = api
    # Each "ó" becomes "%C3%B3" (6 characters), so the stored URL is 24 + 6n long.
    prefix = "https://usm.cl/postulaci"
    fits = prefix + "ó" * ((MAX_URL_LENGTH - len(prefix)) // 6)
    too_long = fits + "ó"
    assert len(too_long) < MAX_URL_LENGTH  # the received text alone would pass

    created = client.post("/urls", json={"long_url": fits})
    assert created.status_code == 201
    assert len(created.json()["long_url"]) <= MAX_URL_LENGTH
    assert client.post("/urls", json={"long_url": too_long}).status_code == 400


@pytest.mark.parametrize(
    ("long_url", "status_code"),
    [
        ("https://testserver.local/eRgfR", 400),
        ("HTTPS://TESTSERVER.LOCAL:443/x", 400),
        ("https://testserver.local:8444/eRgfR", 201),
        ("http://testserver.local/x", 201),
    ],
)
def test_urls_of_the_shortener_itself_are_rejected(api, long_url: str, status_code: int) -> None:
    client, _, _ = api

    assert client.post("/urls", json={"long_url": long_url}).status_code == status_code


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_v2_logs_only_the_lookups_that_miss_the_cache(tmp_path: Path) -> None:
    # LiteStar reconfigures the root logger on startup, so the handler goes on the module logger.
    app_logger, handler = logging.getLogger("shortener.app"), ListHandler()
    settings = Settings(architecture="v2", database_path=tmp_path / "urls.sqlite3")
    with TestClient(create_app(settings)) as client:
        code = client.post("/urls", json={"long_url": "https://usm.cl/"}).json()["hash"]
        app_logger.addHandler(handler)
        try:
            for _ in range(3):
                assert client.get(f"/{code}", follow_redirects=False).status_code == 301
        finally:
            app_logger.removeHandler(handler)

    assert handler.messages == [f"Resolved {code} from Shortener DB"]


def test_expired_store_entries_are_purged_without_being_read() -> None:
    async def scenario() -> tuple[bool, bool]:
        store = MemoryStore()
        await store.set("expired", b"x", expires_in=timedelta(milliseconds=1))
        await store.set("fresh", b"x", expires_in=60)
        await asyncio.sleep(0.01)
        lifespan = purge_expired_periodically(store, interval=0.01)
        async with lifespan(Mock()):
            await asyncio.sleep(0.05)
        return await store.exists("expired"), await store.exists("fresh")

    assert asyncio.run(scenario()) == (False, True)


def test_analytics_failure_returns_503(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "urls.sqlite3")
    logger = Mock(spec=HitLogger)
    logger.record.side_effect = AnalyticsUnavailable
    with TestClient(create_app(settings, hit_logger=logger)) as client:
        created = client.post("/urls", json={"long_url": "https://usm.cl/"}).json()

        response = client.get(f"/{created['hash']}", follow_redirects=False)

    assert response.status_code == 503
    assert "location" not in response.headers


@pytest.mark.parametrize(("architecture", "status_code"), [("v1", 503), ("v2", 200)])
def test_health_reports_analytics_only_when_v1_depends_on_it(
    tmp_path: Path, architecture: str, status_code: int
) -> None:
    settings = Settings(architecture=architecture, database_path=tmp_path / "urls.sqlite3")
    logger = Mock(spec=HitLogger)
    logger.ping.side_effect = AnalyticsUnavailable
    with TestClient(create_app(settings, hit_logger=logger)) as client:
        assert client.get("/health").status_code == status_code


def test_v2_cache_avoids_reads_and_expires(tmp_path: Path) -> None:
    repository = SQLiteURLRepository(tmp_path / "urls.sqlite3")
    record = repository.create("https://usm.cl/")
    settings = Settings(architecture="v2", cache_ttl=1)

    with (
        patch.object(repository, "get", wraps=repository.get) as read,
        TestClient(create_app(settings, repository=repository)) as client,
    ):
        for _ in range(2):
            assert client.get(f"/{record.hash}", follow_redirects=False).status_code == 301
        assert read.call_count == 1

        for query in ("?utm_source=a", "?utm_source=b"):
            assert client.get(f"/{record.hash}{query}", follow_redirects=False).status_code == 301
        assert read.call_count == 1

        time.sleep(1.1)
        assert client.get(f"/{record.hash}", follow_redirects=False).status_code == 301
        assert read.call_count == 2


def test_v2_does_not_cache_missing_urls(tmp_path: Path) -> None:
    settings = Settings(architecture="v2", database_path=tmp_path / "urls.sqlite3")
    code = encode_base62(INITIAL_ID)

    with TestClient(create_app(settings)) as client:
        assert client.get(f"/{code}", follow_redirects=False).status_code == 404
        created = client.post("/urls", json={"long_url": "https://usm.cl/"}).json()
        assert created["hash"] == code
        assert client.get(f"/{code}", follow_redirects=False).status_code == 301


def test_v2_rate_limit_applies_to_cached_redirects_and_excludes_health(tmp_path: Path) -> None:
    repository = SQLiteURLRepository(tmp_path / "urls.sqlite3")
    record = repository.create("https://usm.cl/")
    settings = Settings(architecture="v2", rate_limit_requests=2)

    with TestClient(create_app(settings, repository=repository)) as client:
        for _ in range(2):
            response = client.get(f"/{record.hash}", follow_redirects=False)
            assert response.status_code == 301
            assert "ratelimit-remaining" in response.headers
        assert client.get(f"/{record.hash}", follow_redirects=False).status_code == 429
        assert client.get("/health").status_code == 200


def test_v2_rate_limit_counts_simultaneous_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_time = rate_limit.time()
    monkeypatch.setattr(rate_limit, "time", lambda: fixed_time)
    repository = SQLiteURLRepository(tmp_path / "urls.sqlite3")
    record = repository.create("https://usm.cl/")
    settings = Settings(architecture="v2", rate_limit_requests=3)
    barrier = Barrier(8)

    with TestClient(create_app(settings, repository=repository)) as client:

        def request_redirect(_: int) -> int:
            barrier.wait(timeout=5)
            return client.get(f"/{record.hash}", follow_redirects=False).status_code

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(request_redirect, range(8)))

    assert Counter(statuses) == {301: 3, 429: 5}
