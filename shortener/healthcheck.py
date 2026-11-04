import ssl
import sys
import urllib.request

import grpc

from shortener.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    ca_path = settings.tls_directory / "ca.crt"
    if sys.argv[1:] == ["grpc"]:
        credentials = grpc.ssl_channel_credentials(root_certificates=ca_path.read_bytes())
        with grpc.secure_channel("localhost:50051", credentials) as channel:
            grpc.channel_ready_future(channel).result(timeout=3)
    else:
        context = ssl.create_default_context(cafile=str(ca_path))
        with urllib.request.urlopen(
            "https://localhost:8000/health", context=context, timeout=3
        ) as response:
            if response.status != 200:
                raise RuntimeError("HTTP health check failed")


if __name__ == "__main__":
    main()
