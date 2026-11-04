import pytest
from pydantic import ValidationError

from shortener.settings import Settings


@pytest.mark.parametrize(
    "values",
    [
        {"architecture": "v3"},
        {"cache_ttl": 0},
        {"rate_limit_requests": -1},
        {"grpc_timeout": 0},
        {"public_base_url": "http://localhost"},
        {"public_base_url": "https://localhost/path"},
        {"public_base_url": "https://user:password@localhost"},
    ],
)
def test_invalid_settings_fail_early(values) -> None:
    with pytest.raises(ValidationError):
        Settings(**values)


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHITECTURE", "v2")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "15")

    settings = Settings.from_env()

    assert settings.architecture == "v2"
    assert settings.rate_limit_requests == 15
