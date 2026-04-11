FROM python:3.13.2-slim-bullseye

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    fluidsynth \
    fluid-soundfont-gm \
    vorbis-tools \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

WORKDIR /app

RUN uv sync --no-dev --frozen --no-cache

ENV PATH="/app/.venv/bin:$PATH"
ENV SOUNDFONT="/usr/share/sounds/sf2/FluidR3_GM.sf2"

ENTRYPOINT [ "bash", "/app/src/scripts/entry" ]
