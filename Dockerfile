# Drok core: web UI, agent runtime, scheduler, Telegram. Node is included for npx-based MCP servers.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd -m -u 1000 drok && mkdir -p /data /workspace && chown drok:drok /data /workspace
USER drok
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DROK_DATA_DIR=/data DROK_WORKSPACE=/workspace
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=4)"
CMD ["drok"]
