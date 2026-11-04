import uvicorn

from shortener.app import create_app
from shortener.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8000,
        ssl_keyfile=str(settings.tls_directory / "server.key"),
        ssl_certfile=str(settings.tls_directory / "server.crt"),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
