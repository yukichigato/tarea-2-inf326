import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

import grpc
from google.protobuf.empty_pb2 import Empty
from pydantic import HttpUrl, TypeAdapter, ValidationError

from shortener.domain import UrlHit, is_base62
from shortener.rpc import hits_pb2, hits_pb2_grpc
from shortener.settings import Settings
from shortener.storage import SQLiteHitRepository

# Same limit as domain.MAX_URL_LENGTH, which the shortener applies before storing a URL.
HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)
PORT = 50051

# Fixed name: run with ``python -m``, this module's __name__ would be "__main__".
logger = logging.getLogger("shortener.log_service")


class HitLogService(hits_pb2_grpc.HitLogServicer):
    def __init__(self, repository: SQLiteHitRepository) -> None:
        self.repository = repository

    def RecordHit(self, request: hits_pb2.UrlHit, context: grpc.ServicerContext) -> Empty:
        try:
            if not is_base62(request.hash):
                raise ValueError("Missing or invalid hash")
            HTTP_URL_ADAPTER.validate_python(request.url)
            if not request.HasField("visited_at"):
                raise ValueError("Missing timestamp")
            visited_at = request.visited_at.ToDatetime(tzinfo=UTC)
        except (ValidationError, ValueError, OverflowError) as error:
            logger.warning("Rejected an invalid url hit for %r: %s", request.hash, error)
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "A base62 hash, a valid HTTP URL and a timestamp are required",
            )
        if not context.is_active():
            # The caller already gave up (deadline or cancellation) and answered 503,
            # so writing now would record a visit that never got its redirect.
            logger.warning("Discarded the url hit for %s: the caller stopped waiting", request.hash)
            context.abort(grpc.StatusCode.CANCELLED, "The caller is no longer waiting")
        hit = UrlHit(request.hash, request.url, visited_at)
        self.repository.record(hit)
        logger.info(
            "url hit recorded: hash=%s url=%s visited_at=%s",
            hit.hash,
            hit.url,
            hit.visited_at.isoformat(),
        )
        return Empty()


def build_log_server(
    repository: SQLiteHitRepository,
    address: str,
    credentials: grpc.ServerCredentials | None = None,
) -> tuple[grpc.Server, int]:
    server = grpc.server(ThreadPoolExecutor(max_workers=8))
    hits_pb2_grpc.add_HitLogServicer_to_server(HitLogService(repository), server)
    port = (
        server.add_secure_port(address, credentials)
        if credentials is not None
        else server.add_insecure_port(address)
    )
    if not port:
        raise RuntimeError(f"Could not listen on {address}")
    return server, port


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    settings = Settings.from_env()
    credentials = grpc.ssl_server_credentials(
        (
            (
                (settings.tls_directory / "server.key").read_bytes(),
                (settings.tls_directory / "server.crt").read_bytes(),
            ),
        )
    )
    server, _ = build_log_server(
        SQLiteHitRepository(settings.database_path), f"[::]:{PORT}", credentials
    )
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(shutdown_signal, lambda *_: server.stop(grace=5))
    server.start()
    logger.info("Log listening for url hits on port %d (TLS)", PORT)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
