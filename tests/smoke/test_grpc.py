import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import ANY, Mock

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from litestar.testing import TestClient

from shortener.analytics import GrpcHitLogger
from shortener.app import create_app
from shortener.certificates import generate_certificates
from shortener.domain import MAX_URL_LENGTH, AnalyticsUnavailable, UrlHit, encode_base62
from shortener.log_service import HitLogService, build_log_server
from shortener.rpc import hits_pb2, hits_pb2_grpc
from shortener.settings import Settings
from shortener.storage import SQLiteHitRepository

pytestmark = pytest.mark.smoke


@pytest.fixture
def log_server(tmp_path: Path):
    certificate_directory = tmp_path / "certs"
    generate_certificates(certificate_directory)
    credentials = grpc.ssl_server_credentials(
        (
            (
                (certificate_directory / "server.key").read_bytes(),
                (certificate_directory / "server.crt").read_bytes(),
            ),
        )
    )
    database_path = tmp_path / "hits.sqlite3"
    server, port = build_log_server(SQLiteHitRepository(database_path), "localhost:0", credentials)
    server.start()
    try:
        yield f"localhost:{port}", certificate_directory / "ca.crt", database_path
    finally:
        server.stop(grace=0).wait()


def send_raw_hit(log_server, hit: hits_pb2.UrlHit) -> None:
    target, ca_path, _ = log_server
    credentials = grpc.ssl_channel_credentials(root_certificates=ca_path.read_bytes())
    with grpc.secure_channel(target, credentials) as channel:
        hits_pb2_grpc.HitLogStub(channel).RecordHit(hit, timeout=2)


def now_timestamp() -> Timestamp:
    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    return timestamp


def test_grpc_records_a_timestamped_visit_over_tls(
    log_server, caplog: pytest.LogCaptureFixture
) -> None:
    target, ca_path, database_path = log_server
    logger = GrpcHitLogger(target, ca_path, timeout=2)
    visited_at = datetime(2026, 11, 4, 12, tzinfo=UTC)
    try:
        with caplog.at_level(logging.INFO, logger="shortener.log_service"):
            logger.record(UrlHit("eRgfR", "https://usm.cl/", visited_at))
    finally:
        logger.close()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT hash, url, visited_at FROM hits").fetchall()
    assert rows == [("eRgfR", "https://usm.cl/", visited_at.isoformat())]
    assert (
        "url hit recorded: hash=eRgfR url=https://usm.cl/ visited_at=2026-11-04T12:00:00+00:00"
        in caplog.messages
    )


@pytest.mark.parametrize("url", ["", "ftp://usm.cl/"])
def test_grpc_rejects_invalid_urls(log_server, url: str) -> None:
    with pytest.raises(grpc.RpcError) as error:
        send_raw_hit(log_server, hits_pb2.UrlHit(hash="eRgfR", url=url, visited_at=now_timestamp()))
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize("code", ["", "eR-fR"])
def test_grpc_requires_a_base62_hash(log_server, code: str) -> None:
    with pytest.raises(grpc.RpcError) as error:
        send_raw_hit(
            log_server,
            hits_pb2.UrlHit(hash=code, url="https://usm.cl/", visited_at=now_timestamp()),
        )
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_grpc_requires_a_timestamp(log_server) -> None:
    with pytest.raises(grpc.RpcError) as error:
        send_raw_hit(log_server, hits_pb2.UrlHit(hash="eRgfR", url="https://usm.cl/"))
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_grpc_failure_uses_the_configured_deadline_and_logs_the_cause(
    log_server, caplog: pytest.LogCaptureFixture
) -> None:
    _, ca_path, _ = log_server
    logger = GrpcHitLogger("localhost:1", ca_path, timeout=0.1)
    try:
        with pytest.raises(AnalyticsUnavailable) as error:
            logger.record(UrlHit("eRgfR", "https://usm.cl/", datetime.now(UTC)))
    finally:
        logger.close()
    code = error.value.__cause__.code().name
    assert any(
        message.startswith(f"Log did not confirm the visit to eRgfR: {code}")
        for message in caplog.messages
    )


def test_log_discards_a_hit_whose_caller_stopped_waiting(tmp_path: Path) -> None:
    repository = Mock(spec=SQLiteHitRepository)
    context = Mock(spec=grpc.ServicerContext)
    context.is_active.return_value = False
    context.abort.side_effect = RuntimeError("aborted")
    request = hits_pb2.UrlHit(hash="eRgfR", url="https://usm.cl/", visited_at=now_timestamp())

    with pytest.raises(RuntimeError, match="aborted"):
        HitLogService(repository).RecordHit(request, context)

    repository.record.assert_not_called()
    context.abort.assert_called_once_with(grpc.StatusCode.CANCELLED, ANY)


def test_every_url_the_shortener_accepts_is_also_accepted_by_log(log_server) -> None:
    target, ca_path, database_path = log_server
    logger = GrpcHitLogger(target, ca_path, timeout=2)
    settings = Settings(
        database_path=database_path.with_name("urls.sqlite3"),
        public_base_url="https://testserver.local",
    )
    # The longest URL whose normalized form still fits: each "ó" becomes "%C3%B3".
    prefix = "https://usm.cl/postulaci"
    longest = prefix + "ó" * ((MAX_URL_LENGTH - len(prefix)) // 6)
    try:
        with TestClient(
            create_app(settings, hit_logger=logger), base_url="https://testserver.local"
        ) as client:
            created = client.post("/urls", json={"long_url": longest})
            assert created.status_code == 201
            visit = client.get(f"/{created.json()['hash']}", follow_redirects=False)
            assert visit.status_code == 302
            assert client.post("/urls", json={"long_url": longest + "ó"}).status_code == 400
    finally:
        logger.close()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT hash, url FROM hits").fetchall()
    assert rows == [(created.json()["hash"], created.json()["long_url"])]


def test_grpc_ping_reports_reachability(log_server) -> None:
    target, ca_path, _ = log_server
    reachable = GrpcHitLogger(target, ca_path, timeout=2)
    unreachable = GrpcHitLogger("localhost:1", ca_path, timeout=0.1)
    try:
        reachable.ping()
        with pytest.raises(AnalyticsUnavailable):
            unreachable.ping()
    finally:
        reachable.close()
        unreachable.close()


def test_failed_ping_releases_the_readiness_subscription(
    log_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ca_path, _ = log_server
    readiness = Mock()
    readiness.result.side_effect = grpc.FutureTimeoutError
    monkeypatch.setattr(grpc, "channel_ready_future", lambda channel: readiness)
    logger = GrpcHitLogger("localhost:1", ca_path, timeout=0.1)
    try:
        with pytest.raises(AnalyticsUnavailable):
            logger.ping()
    finally:
        logger.close()
    readiness.cancel.assert_called_once_with()


def test_grpc_timeout_can_be_followed_by_a_committed_event(
    log_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, ca_path, database_path = log_server
    received = Event()
    release_write = Event()
    committed = Event()
    original_record = SQLiteHitRepository.record

    def delayed_record(repository: SQLiteHitRepository, hit: UrlHit) -> None:
        received.set()
        if not release_write.wait(timeout=5):
            raise TimeoutError("The test did not release the pending write")
        original_record(repository, hit)
        committed.set()

    monkeypatch.setattr(SQLiteHitRepository, "record", delayed_record)
    logger = GrpcHitLogger(target, ca_path, timeout=0.2)
    try:
        grpc.channel_ready_future(logger.channel).result(timeout=3)
        with pytest.raises(AnalyticsUnavailable) as error:
            logger.record(UrlHit("eRgfR", "https://usm.cl/", datetime.now(UTC)))
        assert received.is_set()
        assert error.value.__cause__.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        release_write.set()
        assert committed.wait(timeout=3)
        with closing(sqlite3.connect(database_path)) as connection:
            assert connection.execute("SELECT url FROM hits").fetchall() == [("https://usm.cl/",)]
    finally:
        release_write.set()
        logger.close()


def test_concurrent_grpc_visits_are_all_persisted(log_server) -> None:
    target, ca_path, database_path = log_server
    logger = GrpcHitLogger(target, ca_path, timeout=2)
    hits = [
        UrlHit(encode_base62(index), f"https://usm.cl/campaign/{index}", datetime.now(UTC))
        for index in range(16)
    ]
    try:
        grpc.channel_ready_future(logger.channel).result(timeout=3)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(logger.record, hits))
    finally:
        logger.close()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT hash, url FROM hits").fetchall()
    assert sorted(rows) == sorted((hit.hash, hit.url) for hit in hits)
