# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13.14
ARG NODE_VERSION=24.13.0
ARG DENO_VERSION=2.9.2
ARG UV_VERSION=0.8.22

FROM node:${NODE_VERSION}-bookworm-slim AS node
FROM denoland/deno:bin-${DENO_VERSION} AS deno
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS python-runtime

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="cfwarp-evaluation-runtime" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

COPY --from=uv /uv /uvx /usr/local/bin/

ENV HOME=/home/probe \
    CFWARP_EVAL_CONTRACTS_ROOT=/app/contracts \
    UV_PROJECT_ENVIRONMENT=/opt/service-eval-venv \
    PATH=/opt/service-eval-venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    /usr/sbin/useradd --uid 10001 --home-dir /home/probe --create-home --shell /usr/sbin/nologin probe && \
    install -d -o probe -g probe -m 0755 /app/service-eval /var/lib/cfwarp-brush

WORKDIR /app/service-eval
COPY contracts /app/contracts
COPY service-eval/pyproject.toml service-eval/uv.lock service-eval/README.md ./
COPY service-eval/src ./src
COPY service-eval/browser ./browser
RUN uv sync --frozen --no-dev --no-editable && \
    curl --version >/dev/null && \
    chown -R probe:probe /app/service-eval /opt/service-eval-venv /var/lib/cfwarp-brush

USER 10001:10001

FROM python-runtime AS observer
LABEL io.cfwarp.component="observer"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"
ENTRYPOINT ["cfwarp-service-eval-api"]

FROM python-runtime AS worker
LABEL io.cfwarp.component="worker"
ENTRYPOINT ["cfwarp-eval-worker"]

FROM python-runtime AS browser-runtime

ARG AGENT_BROWSER_VERSION=0.31.2

USER root
COPY --from=node /usr/local /usr/local
COPY --from=deno /deno /usr/local/bin/deno
ENV XDG_CACHE_HOME=/opt/probe-cache \
    AGENT_BROWSER_RUNTIME_HOME=/tmp \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/local/bin/agent-browser-chrome \
    AGENT_BROWSER_DOWNLOADS_DISABLED=1
COPY service-eval/container/chrome-policy.json /etc/opt/chrome/policies/managed/cfwarp-download-policy.json
COPY service-eval/container/chrome-policy.json /etc/chromium/policies/managed/cfwarp-download-policy.json
RUN npm install --global "agent-browser@${AGENT_BROWSER_VERSION}" && \
    install -d -o probe -g probe -m 0755 /opt/probe-cache && \
    HOME=/home/probe XDG_CACHE_HOME=/opt/probe-cache agent-browser install --with-deps && \
    ln -s "$(find /home/probe/.agent-browser/browsers -mindepth 2 -maxdepth 2 -type f -name chrome -print -quit)" /usr/local/bin/agent-browser-chrome && \
    deno check --config browser/deno.json browser/src/main.ts && \
    chown -R probe:probe /home/probe /opt/probe-cache
USER 10001:10001

FROM browser-runtime AS browser
LABEL io.cfwarp.component="browser-worker"
ENTRYPOINT ["cfwarp-eval-worker"]

# Temporary migration/rollback image retaining the historical API entrypoint
# and browser runtime. New node deployments use the dedicated targets above.
FROM browser-runtime AS full
LABEL io.cfwarp.component="legacy-full"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"
ENTRYPOINT ["cfwarp-service-eval-api"]
