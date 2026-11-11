from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
INITIAL_ID = 597652313


def encode_base62(number: int) -> str:
    if number < 0:
        raise ValueError("Base62 requires a nonnegative integer")
    if number == 0:
        return BASE62_ALPHABET[0]
    digits = []
    while number:
        number, remainder = divmod(number, len(BASE62_ALPHABET))
        digits.append(BASE62_ALPHABET[remainder])
    return "".join(reversed(digits))


@dataclass(frozen=True)
class ShortURL:
    id: int
    hash: str
    long_url: str


class URLRepository(Protocol):
    def create(self, long_url: str) -> ShortURL: ...

    def get(self, code: str) -> ShortURL | None: ...

    def ping(self) -> None: ...


class HitLogger(Protocol):
    def record(self, url: str, visited_at: datetime) -> None: ...

    def ping(self) -> None: ...


class UnknownHash(Exception):
    pass


class AnalyticsUnavailable(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ShortenerService:
    repository: URLRepository
    hit_logger: HitLogger | None = None
    clock: Callable[[], datetime] = utc_now

    def create(self, long_url: str) -> ShortURL:
        return self.repository.create(long_url)

    def resolve(self, code: str) -> str:
        record = self.repository.get(code)
        if record is None:
            raise UnknownHash(code)
        if self.hit_logger is not None:
            self.hit_logger.record(record.long_url, self.clock())
        return record.long_url

    def check_health(self) -> None:
        self.repository.ping()
        if self.hit_logger is not None:
            self.hit_logger.ping()
