from datetime import datetime
from pathlib import Path

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from shortener.domain import AnalyticsUnavailable
from shortener.rpc import hits_pb2, hits_pb2_grpc


class GrpcHitLogger:
    def __init__(self, target: str, ca_path: Path, timeout: float) -> None:
        credentials = grpc.ssl_channel_credentials(root_certificates=ca_path.read_bytes())
        self.channel = grpc.secure_channel(target, credentials)
        self.stub = hits_pb2_grpc.HitLogStub(self.channel)
        self.timeout = timeout

    def record(self, url: str, visited_at: datetime) -> None:
        timestamp = Timestamp()
        timestamp.FromDatetime(visited_at)
        try:
            self.stub.RecordHit(
                hits_pb2.UrlHit(url=url, visited_at=timestamp), timeout=self.timeout
            )
        except grpc.RpcError as error:
            raise AnalyticsUnavailable(
                "The analytics service could not record the visit"
            ) from error

    def close(self) -> None:
        self.channel.close()
