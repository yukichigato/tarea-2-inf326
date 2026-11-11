import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

import grpc
from google.protobuf.empty_pb2 import Empty
from pydantic import HttpUrl, TypeAdapter, ValidationError

from shortener.rpc import hits_pb2, hits_pb2_grpc
from shortener.settings import Settings
from shortener.storage import SQLiteHitRepository

HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


class HitLogService(hits_pb2_grpc.HitLogServicer):
    def __init__(self, repository: SQLiteHitRepository) -> None:
        self.repository = repository

    def RecordHit(self, request: hits_pb2.UrlHit, context: grpc.ServicerContext) -> Empty:
        try:
            HTTP_URL_ADAPTER.validate_python(request.url)
            if not request.HasField("visited_at"):
                raise ValueError("Missing timestamp")
            visited_at = request.visited_at.ToDatetime(tzinfo=UTC)
        except (ValidationError, ValueError, OverflowError):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "A valid HTTP URL and timestamp are required"
            )
        self.repository.record(request.url, visited_at)
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
        SQLiteHitRepository(settings.database_path), "[::]:50051", credentials
    )
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(shutdown_signal, lambda *_: server.stop(grace=5))
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    main()
