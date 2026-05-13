# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.14-slim-bookworm AS runtime
WORKDIR /app

LABEL org.opencontainers.image.title="chs-tile-proxy" \
      org.opencontainers.image.description="Caching XYZ tile proxy for Canadian Hydrographic Service ENC charts" \
      org.opencontainers.image.licenses="MIT"

# UID 99 / GID 100 matches Unraid's default nobody:users so a host bind
# mount under /mnt/user/appdata/... is writable without extra chowning.
RUN groupadd --system --gid 100 app 2>/dev/null || groupmod -n app "$(getent group 100 | cut -d: -f1)" && \
    useradd --system --uid 99 --gid 100 --home /app --shell /usr/sbin/nologin app
RUN mkdir -p /var/cache/chs_tiles && chown 99:100 /var/cache/chs_tiles

COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:${PATH}" \
    CACHE_DIR=/var/cache/chs_tiles \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1

USER app
EXPOSE 8001
VOLUME ["/var/cache/chs_tiles"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=3).status==200 else 1)"

# Run via sh so FORWARDED_ALLOW_IPS expands at container start. Set it to
# the reverse-proxy IP (or "*" if you fully trust the network) when fronted
# by Caddy/nginx/Traefik so rate limiting sees real client IPs.
CMD ["sh", "-c", "exec uvicorn chs_proxy.main:app --host 0.0.0.0 --port 8001 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS}\""]
