FROM python:3.13.2-slim-bullseye

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    fluidsynth \
    fluid-soundfont-gm \
    opus-tools \
    libgl1 \
    wget \
    bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Ren'Py SDK для lint/compile (headless)
RUN wget -q https://www.renpy.org/dl/8.5.2/renpy-8.5.2-sdk.tar.bz2 \
    && tar xf renpy-8.5.2-sdk.tar.bz2 \
    && mv renpy-8.5.2-sdk /opt/renpy \
    && rm renpy-8.5.2-sdk.tar.bz2

COPY . /app

WORKDIR /app

RUN uv sync --no-dev --frozen --no-cache

ENV PATH="/app/.venv/bin:/opt/renpy:$PATH"
ENV SOUNDFONT="/usr/share/sounds/sf2/FluidR3_GM.sf2"

ENTRYPOINT [ "bash", "/app/src/scripts/entry" ]
