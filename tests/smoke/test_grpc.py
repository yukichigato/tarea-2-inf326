import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from shortener.analytics import GrpcHitLogger
from shortener.certificates import generate_certificates
from shortener.domain import AnalyticsUnavailable
from shortener.log_service import build_log_server
from shortener.rpc import hits_pb2, hits_pb2_grpc
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


def test_grpc_records_a_timestamped_visit_over_tls(log_server) -> None:
    target, ca_path, database_path = log_server
    logger = GrpcHitLogger(target, ca_path, timeout=2)
    visited_at = datetime(2026, 11, 4, 12, tzinfo=UTC)
    try:
        logger.record("https://usm.cl/", visited_at)
    finally:
        logger.close()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT url, visited_at FROM hits").fetchall()
    assert rows == [("https://usm.cl/", visited_at.isoformat())]


@pytest.mark.parametrize("url", ["", "ftp://usm.cl/"])
def test_grpc_rejects_invalid_urls(log_server, url: str) -> None:
    target, ca_path, _ = log_server
    credentials = grpc.ssl_channel_credentials(root_certificates=ca_path.read_bytes())
    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    with (
        grpc.secure_channel(target, credentials) as channel,
        pytest.raises(grpc.RpcError) as error,
    ):
        hits_pb2_grpc.HitLogStub(channel).RecordHit(
            hits_pb2.UrlHit(url=url, visited_at=timestamp), timeout=2
        )
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_grpc_requires_a_timestamp(log_server) -> None:
    target, ca_path, _ = log_server
    credentials = grpc.ssl_channel_credentials(root_certificates=ca_path.read_bytes())
    with (
        grpc.secure_channel(target, credentials) as channel,
        pytest.raises(grpc.RpcError) as error,
    ):
        hits_pb2_grpc.HitLogStub(channel).RecordHit(
            hits_pb2.UrlHit(url="https://usm.cl/"), timeout=2
        )
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_grpc_failure_uses_the_configured_deadline(log_server) -> None:
    _, ca_path, _ = log_server
    logger = GrpcHitLogger("localhost:1", ca_path, timeout=0.1)
    try:
        with pytest.raises(AnalyticsUnavailable):
            logger.record("https://usm.cl/", datetime.now(UTC))
    finally:
        logger.close()


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

    def delayed_record(repository: SQLiteHitRepository, url: str, visited_at: datetime) -> None:
        received.set()
        if not release_write.wait(timeout=5):
            raise TimeoutError("The test did not release the pending write")
        original_record(repository, url, visited_at)
        committed.set()

    monkeypatch.setattr(SQLiteHitRepository, "record", delayed_record)
    logger = GrpcHitLogger(target, ca_path, timeout=0.2)
    try:
        grpc.channel_ready_future(logger.channel).result(timeout=3)
        with pytest.raises(AnalyticsUnavailable) as error:
            logger.record("https://usm.cl/", datetime.now(UTC))
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
    urls = [f"https://usm.cl/campaign/{index}" for index in range(16)]
    try:
        grpc.channel_ready_future(logger.channel).result(timeout=3)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda url: logger.record(url, datetime.now(UTC)), urls))
    finally:
        logger.close()

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("SELECT url FROM hits").fetchall()
    assert sorted(row[0] for row in rows) == sorted(urls)
