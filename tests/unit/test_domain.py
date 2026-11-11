from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from shortener.domain import (
    AnalyticsUnavailable,
    HitLogger,
    ShortenerService,
    ShortURL,
    UnknownHash,
    URLRepository,
    encode_base62,
)


@pytest.mark.parametrize(
    ("number", "expected"), [(0, "0"), (9, "9"), (10, "A"), (61, "z"), (62, "10")]
)
def test_base62_encoding(number: int, expected: str) -> None:
    assert encode_base62(number) == expected


def test_base62_rejects_negative_numbers() -> None:
    with pytest.raises(ValueError):
        encode_base62(-1)


def test_resolving_url_records_the_destination_and_injected_time() -> None:
    repository = Mock(spec=URLRepository)
    repository.get.return_value = ShortURL(1, "1", "https://usm.cl/")
    logger = Mock(spec=HitLogger)
    visited_at = datetime(2026, 11, 4, tzinfo=UTC)
    service = ShortenerService(repository, logger, clock=lambda: visited_at)

    assert service.resolve("1") == "https://usm.cl/"
    logger.record.assert_called_once_with("https://usm.cl/", visited_at)


def test_missing_url_does_not_record_a_visit() -> None:
    repository = Mock(spec=URLRepository)
    repository.get.return_value = None
    logger = Mock(spec=HitLogger)

    with pytest.raises(UnknownHash):
        ShortenerService(repository, logger).resolve("missing")

    logger.record.assert_not_called()


def test_logging_failure_is_propagated() -> None:
    repository = Mock(spec=URLRepository)
    repository.get.return_value = ShortURL(1, "1", "https://usm.cl/")
    logger = Mock(spec=HitLogger)
    logger.record.side_effect = AnalyticsUnavailable

    with pytest.raises(AnalyticsUnavailable):
        ShortenerService(repository, logger).resolve("1")


def test_resolving_without_analytics_only_needs_the_repository() -> None:
    repository = Mock(spec=URLRepository)
    repository.get.return_value = ShortURL(1, "1", "https://usm.cl/")

    assert ShortenerService(repository).resolve("1") == "https://usm.cl/"


def test_health_check_verifies_repository_and_analytics() -> None:
    repository = Mock(spec=URLRepository)
    logger = Mock(spec=HitLogger)

    ShortenerService(repository, logger).check_health()

    repository.ping.assert_called_once_with()
    logger.ping.assert_called_once_with()


def test_health_check_propagates_unavailable_analytics() -> None:
    logger = Mock(spec=HitLogger)
    logger.ping.side_effect = AnalyticsUnavailable

    with pytest.raises(AnalyticsUnavailable):
        ShortenerService(Mock(spec=URLRepository), logger).check_health()
