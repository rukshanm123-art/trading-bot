# trading-bot — non-root, minimal-privilege container.
# Starts in PAPER mode by default; LIVE cannot start without the full unlock
# chain regardless of how this container is launched.

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --create-home bot

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY migrations ./migrations
COPY config ./config
COPY scripts ./scripts
COPY data/fixtures ./data/fixtures
RUN pip install --no-cache-dir --no-deps . && \
    mkdir -p /app/var && chown -R bot:bot /app/var

USER bot

# Readiness/liveness endpoints (monitoring server binds inside the container).
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9754/health/live', timeout=3).status==200 else 1)" \
    || exit 1

# Default: migrate then run PAPER mode. Never a live entrypoint by default.
CMD ["sh", "-c", "python -m trading_bot --config config/paper.yaml db migrate && python -m trading_bot --config config/paper.yaml paper run"]
