from litestar import Litestar, get, post
from litestar.config.response_cache import ResponseCacheConfig
from litestar.di import NamedDependency, Provide
from litestar.exceptions import HTTPException, NotFoundException
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.params import FromPath, JSONBody
from litestar.response import Redirect
from litestar.stores.memory import MemoryStore
from pydantic import BaseModel, HttpUrl

from shortener.analytics import GrpcHitLogger
from shortener.domain import (
    AnalyticsUnavailable,
    HitLogger,
    ShortenerService,
    UnknownHash,
    URLRepository,
)
from shortener.settings import Settings
from shortener.storage import SQLiteURLRepository


class CreateURL(BaseModel):
    long_url: HttpUrl


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
    settings = settings if settings is not None else Settings.from_env()
    repository = (
        repository if repository is not None else SQLiteURLRepository(settings.database_path)
    )
    shutdown_hooks = []
    if settings.architecture == "v1" and hit_logger is None:
        grpc_logger = GrpcHitLogger(
            settings.grpc_target, settings.tls_directory / "ca.crt", settings.grpc_timeout
        )
        hit_logger = grpc_logger
        shutdown_hooks.append(grpc_logger.close)
    service = ShortenerService(repository, hit_logger if settings.architecture == "v1" else None)

    @post("/urls", sync_to_thread=True, status_code=201)
    def create_url(
        data: JSONBody[CreateURL], shortener: NamedDependency[ShortenerService]
    ) -> CreatedURL:
        record = shortener.create(str(data.long_url))
        return CreatedURL(
            id=record.id,
            hash=record.hash,
            long_url=record.long_url,
            short_url=f"{str(settings.public_base_url).rstrip('/')}/{record.hash}",
        )

    @get(
        "/{code:str}",
        sync_to_thread=True,
        cache=settings.cache_ttl if settings.architecture == "v2" else False,
    )
    def redirect_url(code: FromPath[str], shortener: NamedDependency[ShortenerService]) -> Redirect:
        try:
            long_url = shortener.resolve(code)
        except UnknownHash as error:
            raise NotFoundException(detail="Unknown short URL") from error
        except AnalyticsUnavailable as error:
            raise HTTPException(status_code=503, detail="Analytics service unavailable") from error
        return Redirect(
            path=long_url,
            status_code=302 if settings.architecture == "v1" else 301,
            headers={
                "Cache-Control": (
                    "no-store"
                    if settings.architecture == "v1"
                    else f"public, max-age={settings.cache_ttl}"
                )
            },
        )

    @get("/health", sync_to_thread=True, include_in_schema=False)
    def health() -> dict[str, str]:
        repository.ping()
        return {"status": "ok", "architecture": settings.architecture}

    middleware = []
    if settings.architecture == "v2":
        middleware.append(
            RateLimitConfig(
                rate_limit=("minute", settings.rate_limit_requests),
                exclude=[r"^/health$"],
            ).middleware
        )

    return Litestar(
        route_handlers=[create_url, redirect_url, health],
        dependencies={"shortener": Provide(lambda: service, sync_to_thread=False)},
        middleware=middleware,
        stores={"response_cache": MemoryStore(), "rate_limit": MemoryStore()},
        response_cache_config=ResponseCacheConfig(
            cache_response_filter=lambda scope, status_code: status_code == 301
        ),
        on_shutdown=shutdown_hooks,
        openapi_config=None,
    )
