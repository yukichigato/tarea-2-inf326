FROM ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 AS uv

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app

FROM base AS build
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY shortener/ ./shortener/
COPY proto/ ./proto/
RUN uv run --no-sync python -m grpc_tools.protoc -I proto \
    --python_out=. --grpc_python_out=. proto/shortener/rpc/hits.proto

FROM build AS test
COPY tests/ ./tests/
USER app
CMD ["uv", "run", "--no-sync", "pytest"]

FROM build AS production-dependencies
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM base AS runtime
COPY --from=production-dependencies /app/.venv /app/.venv
COPY --from=production-dependencies /app/shortener /app/shortener
RUN mkdir /data /certs && chown app:app /data /certs
USER app
EXPOSE 8000 50051
CMD ["python", "-m", "shortener"]
