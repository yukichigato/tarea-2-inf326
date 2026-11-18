import logging
from pathlib import Path

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from shortener.domain import AnalyticsUnavailable, UrlHit
from shortener.rpc import hits_pb2, hits_pb2_grpc

logger = logging.getLogger(__name__)


class GrpcHitLogger:
    def __init__(self, target: str, ca_path: Path, timeout: float) -> None:
        credentials = grpc.ssl_channel_credentials(root_certificates=ca_path.read_bytes())
        self.channel = grpc.secure_channel(target, credentials)
        self.stub = hits_pb2_grpc.HitLogStub(self.channel)
        self.timeout = timeout

    def record(self, hit: UrlHit) -> None:
        timestamp = Timestamp()
        timestamp.FromDatetime(hit.visited_at)
        try:
            self.stub.RecordHit(
                hits_pb2.UrlHit(hash=hit.hash, url=hit.url, visited_at=timestamp),
                timeout=self.timeout,
            )
        except grpc.RpcError as error:
            logger.warning(
                "Log did not confirm the visit to %s: %s %s",
                hit.hash,
                error.code().name,
                error.details(),
            )
            raise AnalyticsUnavailable(
                "The analytics service could not record the visit"
            ) from error

    def ping(self) -> None:
        readiness = grpc.channel_ready_future(self.channel)
        try:
            readiness.result(timeout=self.timeout)
        except grpc.FutureTimeoutError as error:
            readiness.cancel()
            raise AnalyticsUnavailable("The analytics service is not reachable") from error

    def close(self) -> None:
        self.channel.close()
