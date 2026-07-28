# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13.14
ARG NODE_VERSION=24.13.0
ARG DENO_VERSION=2.9.2
ARG UV_VERSION=0.8.22

FROM node:${NODE_VERSION}-bookworm-slim AS node
FROM denoland/deno:bin-${DENO_VERSION} AS deno
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm

ARG AGENT_BROWSER_VERSION=0.31.2
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="cfwarp-pro-service-eval" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

COPY --from=node /usr/local /usr/local
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=uv /uv /uvx /usr/local/bin/

ENV HOME=/home/probe \
    CFWARP_EVAL_CONTRACTS_ROOT=/app/contracts \
    XDG_CACHE_HOME=/opt/probe-cache \
    AGENT_BROWSER_RUNTIME_HOME=/tmp \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/local/bin/agent-browser-chrome \
    UV_PROJECT_ENVIRONMENT=/opt/service-eval-venv \
    PATH=/opt/service-eval-venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_BROWSER_DOWNLOADS_DISABLED=1

RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates curl sudo && \
    rm -rf /var/lib/apt/lists/* && \
    npm install --global "agent-browser@${AGENT_BROWSER_VERSION}" && \
    install -d -m 0755 /opt/probe-cache /app/service-eval /home/probe /var/lib/cfwarp-brush && \
    HOME=/home/probe XDG_CACHE_HOME=/opt/probe-cache agent-browser install --with-deps && \
    ln -s "$(find /home/probe/.agent-browser/browsers -mindepth 2 -maxdepth 2 -type f -name chrome -print -quit)" /usr/local/bin/agent-browser-chrome && \
    /usr/sbin/useradd --uid 10001 --home-dir /home/probe --shell /usr/sbin/nologin probe && \
    chown -R probe:probe /home/probe /opt/probe-cache /var/lib/cfwarp-brush && \
    SUDO_FORCE_REMOVE=yes apt-get purge --yes sudo && rm -rf /var/lib/apt/lists/*

WORKDIR /app/service-eval
COPY contracts /app/contracts
COPY service-eval/pyproject.toml service-eval/uv.lock service-eval/README.md ./
COPY service-eval/src ./src
COPY service-eval/browser ./browser
COPY service-eval/container/chrome-policy.json /etc/opt/chrome/policies/managed/cfwarp-download-policy.json
COPY service-eval/container/chrome-policy.json /etc/chromium/policies/managed/cfwarp-download-policy.json
RUN uv sync --frozen --no-dev --no-editable && \
    curl --version >/dev/null && \
    deno check --config browser/deno.json browser/src/main.ts && \
    chown -R probe:probe /app/service-eval /opt/service-eval-venv

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"

ENTRYPOINT ["cfwarp-service-eval-api"]
