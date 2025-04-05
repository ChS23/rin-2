FROM python:3.13.2-slim-bullseye

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

COPY . /app

WORKDIR /app

RUN uv sync --no-dev --frozen --no-cache

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT [ "bash", "/app/src/scripts/entry" ]
