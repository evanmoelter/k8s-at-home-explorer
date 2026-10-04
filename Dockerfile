FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv
FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 568 explorer && useradd --uid 568 --gid 568 --no-create-home explorer \
    && mkdir -p /data/corpus && chown -R 568:568 /data
COPY --from=build /app /app
COPY config/repositories.yaml /config/repositories.yaml
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/tmp EXPLORER_HOST=0.0.0.0 EXPLORER_CORPUS_DIR=/data/corpus \
    EXPLORER_REPOSITORIES_FILE=/config/repositories.yaml
USER 568:568
WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["k8s-explorer"]
CMD ["serve"]
