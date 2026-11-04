import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

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
