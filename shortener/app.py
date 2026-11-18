import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress

from litestar import Litestar, get, post
from litestar.config.response_cache import ResponseCacheConfig
from litestar.di import NamedDependency, Provide
from litestar.exceptions import HTTPException, NotFoundException, ValidationException
from litestar.handlers import HTTPRouteHandler
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.params import FromPath, JSONBody
from litestar.response import Redirect
from litestar.stores.memory import MemoryStore
from pydantic import BaseModel, HttpUrl, field_validator

from shortener.analytics import GrpcHitLogger
from shortener.domain import (
    MAX_URL_LENGTH,
    AnalyticsUnavailable,
    HitLogger,
    ShortenerService,
    UnknownHash,
    URLRepository,
)
from shortener.settings import Settings
from shortener.storage import SQLiteURLRepository

# Seconds between purges of expired entries in the in-memory stores of architecture 2.
STORE_PURGE_INTERVAL = 60

logger = logging.getLogger(__name__)


class CreateURL(BaseModel):
    long_url: HttpUrl

    @field_validator("long_url")
    @classmethod
    def fits_after_normalization(cls, value: HttpUrl) -> HttpUrl:
        # Pydantic checks the length of the text it receives, but normalization
        # (punycode, percent-encoding) can make the stored URL longer. Log validates
        # the stored URL, so the shortener rejects what Log would reject later.
        if len(str(value)) > MAX_URL_LENGTH:
            raise ValueError(f"the normalized URL exceeds {MAX_URL_LENGTH} characters")
        return value


class CreatedURL(BaseModel):
    id: int
    hash: str
    long_url: str
    short_url: str


def create_app(
    settings: Settings | None = None,
    repository: URLRepository | None = None,
    hit_logger: HitLogger | None = None,
) -> Litestar:
    """Build the architecture selected by ``settings.architecture``."""
    settings = settings if settings is not None else Settings.from_env()
    if settings.architecture == "v1":
        return create_v1_app(settings, repository, hit_logger)
    return create_v2_app(settings, repository)


def create_v1_app(
    settings: Settings,
    repository: URLRepository | None = None,
    hit_logger: HitLogger | None = None,
) -> Litestar:
    """Architecture 1: temporary 302 redirects; every visit is recorded in Log over gRPC."""
    shutdown_hooks = []
    if hit_logger is None:
        grpc_logger = GrpcHitLogger(
            settings.grpc_target, settings.tls_directory / "ca.crt", settings.grpc_timeout
        )
        hit_logger = grpc_logger
        shutdown_hooks.append(grpc_logger.close)
    service = ShortenerService(_url_repository(settings, repository), hit_logger)
    return Litestar(
        route_handlers=[
            _create_url_handler(settings),
            _redirect_handler(status_code=302, cache_control="no-store", cache=False),
            _health_handler(settings),
        ],
        dependencies=_dependencies(service),
        on_shutdown=shutdown_hooks,
        openapi_config=None,
    )


def create_v2_app(settings: Settings, repository: URLRepository | None = None) -> Litestar:
    """Architecture 2: permanent 301 redirects with a rate limiter and a response cache."""
    service = ShortenerService(_url_repository(settings, repository))
    response_cache, rate_limit = MemoryStore(), MemoryStore()
    return Litestar(
        route_handlers=[
            _create_url_handler(settings),
            _redirect_handler(
                status_code=301,
                cache_control=f"public, max-age={settings.cache_ttl}",
                cache=settings.cache_ttl,
            ),
            _health_handler(settings),
        ],
        dependencies=_dependencies(service),
        middleware=[
            RateLimitConfig(
                rate_limit=("minute", settings.rate_limit_requests),
                exclude=[r"^/health$"],
            ).middleware
        ],
        stores={"response_cache": response_cache, "rate_limit": rate_limit},
        response_cache_config=ResponseCacheConfig(
            key_builder=lambda request: request.url.path,
            cache_response_filter=lambda scope, status_code: status_code == 301,
        ),
        lifespan=[purge_expired_periodically(response_cache, rate_limit)],
        openapi_config=None,
    )


async def purge_expired(*stores: MemoryStore) -> None:
    for store in stores:
        await store.delete_expired()


def purge_expired_periodically(
    *stores: MemoryStore, interval: float = STORE_PURGE_INTERVAL
) -> Callable[[Litestar], AbstractAsyncContextManager[None]]:
    """MemoryStore drops an expired entry only when its key is read again, so without
    this purge every hash and every client IP ever seen would stay in memory."""

    @asynccontextmanager
    async def lifespan(_: Litestar) -> AsyncIterator[None]:
        async def purge_forever() -> None:
            while True:
                await asyncio.sleep(interval)
                await purge_expired(*stores)

        task = asyncio.create_task(purge_forever())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return lifespan


def _url_repository(settings: Settings, repository: URLRepository | None) -> URLRepository:
    return repository if repository is not None else SQLiteURLRepository(settings.database_path)


def _dependencies(service: ShortenerService) -> dict[str, Provide]:
    return {"shortener": Provide(lambda: service, sync_to_thread=False)}


def _is_own_url(url: HttpUrl, settings: Settings) -> bool:
    own = settings.public_base_url
    return (url.scheme, url.host, url.port) == (own.scheme, own.host, own.port)


def _create_url_handler(settings: Settings) -> HTTPRouteHandler:
    @post("/urls", sync_to_thread=True, status_code=201)
    def create_url(
        data: JSONBody[CreateURL], shortener: NamedDependency[ShortenerService]
    ) -> CreatedURL:
        if _is_own_url(data.long_url, settings):
            # A short URL pointing at this shortener would redirect to itself or chain
            # redirects, and in architecture 1 every hop would be recorded as a visit.
            raise ValidationException(detail="URLs of this shortener cannot be shortened")
        record = shortener.create(str(data.long_url))
        return CreatedURL(
            id=record.id,
            hash=record.hash,
            long_url=record.long_url,
            short_url=f"{str(settings.public_base_url).rstrip('/')}/{record.hash}",
        )

    return create_url


def _redirect_handler(status_code: int, cache_control: str, cache: int | bool) -> HTTPRouteHandler:
    @get("/{code:str}", sync_to_thread=True, cache=cache)
    def redirect_url(code: FromPath[str], shortener: NamedDependency[ShortenerService]) -> Redirect:
        try:
            long_url = shortener.resolve(code)
        except UnknownHash as error:
            raise NotFoundException(detail="Unknown short URL") from error
        except AnalyticsUnavailable as error:
            raise HTTPException(
                status_code=503, detail="The visit could not be recorded"
            ) from error
        # In architecture 2 this handler only runs when the response cache misses,
        # so this line shows which visits reached Shortener DB.
        logger.info("Resolved %s from Shortener DB", code)
        return Redirect(
            path=long_url, status_code=status_code, headers={"Cache-Control": cache_control}
        )

    return redirect_url


def _health_handler(settings: Settings) -> HTTPRouteHandler:
    @get("/health", sync_to_thread=True, include_in_schema=False)
    def health(shortener: NamedDependency[ShortenerService]) -> dict[str, str]:
        try:
            shortener.check_health()
        except AnalyticsUnavailable as error:
            raise HTTPException(status_code=503, detail="Analytics service unavailable") from error
        return {"status": "ok", "architecture": settings.architecture}

    return health
