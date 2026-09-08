FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    fluidsynth \
    fluid-soundfont-gm \
    opus-tools \
    libgl1 \
    libatomic1 \
    ffmpeg \
    git \
    wget \
    bzip2 \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Ren'Py SDK для lint/compile/web_build (headless)
RUN wget -q https://www.renpy.org/dl/8.5.2/renpy-8.5.2-sdk.tar.bz2 \
    && tar xf renpy-8.5.2-sdk.tar.bz2 \
    && mv renpy-8.5.2-sdk /opt/renpy \
    && rm renpy-8.5.2-sdk.tar.bz2 \
    && wget -q https://www.renpy.org/dl/8.5.2/renpy-8.5.2-web.zip \
    && unzip -q renpy-8.5.2-web.zip -d /opt/renpy \
    && rm renpy-8.5.2-web.zip

COPY . /app

WORKDIR /app

RUN uv sync --no-dev --frozen --no-cache

ENV PATH="/app/.venv/bin:/opt/renpy:$PATH"
ENV SOUNDFONT="/usr/share/sounds/sf2/FluidR3_GM.sf2"

# Версия кода для даталога (.git в образ не копируется — см. .dockerignore).
# Ставится последним слоем, чтобы смена SHA не инвалидировала кеш сборки.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA

ENTRYPOINT [ "bash", "/app/src/scripts/entry" ]
