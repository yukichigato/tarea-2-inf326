import os
import sqlite3
import ssl
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def destination_url():
    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"INF326 destination reached")

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/destination/{uuid4().hex}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def secure_client():
    context = ssl.create_default_context(cafile="/certs/ca.crt")
    with httpx.Client(verify=context, timeout=5, follow_redirects=False, trust_env=False) as client:
        yield client


def test_v1_https_redirects_and_persists_every_visit(secure_client, destination_url: str) -> None:
    base_url = os.environ["E2E_V1_URL"]
    assert secure_client.get(f"{base_url}/health").status_code == 200
    assert secure_client.post(f"{base_url}/urls", json={"long_url": "invalid"}).status_code == 400
    assert secure_client.get(f"{base_url}/missing{uuid4().hex}").status_code == 404
    started_at = datetime.now(UTC)

    created = secure_client.post(f"{base_url}/urls", json={"long_url": destination_url})
    assert created.status_code == 201
    short_path = urlsplit(created.json()["short_url"]).path
    for _ in range(2):
        redirect = secure_client.get(f"{base_url}{short_path}")
        assert redirect.status_code == 302
        assert redirect.headers["cache-control"] == "no-store"
        assert redirect.headers["location"] == destination_url
        destination = secure_client.get(redirect.headers["location"])
        assert destination.status_code == 200
        assert destination.text == "INF326 destination reached"

    database_path = Path(os.environ["E2E_LOG_DATABASE_PATH"])
    with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT url, visited_at FROM hits WHERE url = ?", (destination_url,)
        ).fetchall()
    assert len(rows) == 2
    for url, visited_at in rows:
        assert url == destination_url
        assert datetime.fromisoformat(visited_at) >= started_at


def test_v2_https_permanent_redirect_and_rate_limit(secure_client, destination_url: str) -> None:
    base_url = os.environ["E2E_V2_URL"]
    assert secure_client.get(f"{base_url}/health").status_code == 200
    created = secure_client.post(f"{base_url}/urls", json={"long_url": destination_url})
    assert created.status_code == 201
    short_path = urlsplit(created.json()["short_url"]).path

    for _ in range(2):
        redirect = secure_client.get(f"{base_url}{short_path}")
        assert redirect.status_code == 301
        assert redirect.headers["cache-control"].startswith("public, max-age=")
        assert redirect.headers["location"] == destination_url
        destination = secure_client.get(redirect.headers["location"])
        assert destination.text == "INF326 destination reached"

    assert secure_client.post(f"{base_url}/urls", json={"long_url": "invalid"}).status_code == 400
    missing = secure_client.get(f"{base_url}/missing{uuid4().hex}")
    assert missing.status_code == 404
    remaining = int(missing.headers["ratelimit-remaining"])
    for _ in range(remaining):
        assert secure_client.get(f"{base_url}{short_path}").status_code == 301
    assert secure_client.get(f"{base_url}{short_path}").status_code == 429
    assert secure_client.get(f"{base_url}/health").status_code == 200
