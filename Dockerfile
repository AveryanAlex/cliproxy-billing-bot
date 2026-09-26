# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.12-slim-bookworm AS build

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
    && /opt/venv/bin/python -c "import cliproxy_billing"

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    DATABASE_URL=sqlite+aiosqlite:////data/billing.db

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /data --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown app:app /data

COPY --from=build /opt/venv /opt/venv

WORKDIR /data
USER app

CMD ["cliproxy-billing-bot"]
