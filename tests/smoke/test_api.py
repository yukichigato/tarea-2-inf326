import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from litestar.testing import TestClient

from shortener.app import create_app
from shortener.domain import INITIAL_ID, AnalyticsUnavailable, HitLogger, encode_base62
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


def test_analytics_failure_returns_503(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "urls.sqlite3")
    logger = Mock(spec=HitLogger)
    logger.record.side_effect = AnalyticsUnavailable
    with TestClient(create_app(settings, hit_logger=logger)) as client:
        created = client.post("/urls", json={"long_url": "https://usm.cl/"}).json()

        response = client.get(f"/{created['hash']}", follow_redirects=False)

    assert response.status_code == 503
    assert "location" not in response.headers


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
