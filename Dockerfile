# Flipstream image: web (uvicorn) and worker (python -m flipstream.worker) share it.
FROM denoland/deno:bin-2.9.8 AS deno

FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DENO_DIR=/tmp/deno \
    DENO_NO_UPDATE_CHECK=1

# ffmpeg: MP3 encoding and the cover crop. deno: the JavaScript runtime yt-dlp
# needs for YouTube (with the yt-dlp-ejs package from yt-dlp[default]).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# /scratch belongs to app so the named volume mounted there starts out writable.
RUN pip install . \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app \
 && install -d -o app -g app /scratch

# CI passes the newest yt-dlp release, so this layer rebuilds whenever there is
# a new one even when the layers above come from cache.
ARG YTDLP_VERSION=
RUN pip install --upgrade "yt-dlp[default]${YTDLP_VERSION:+==$YTDLP_VERSION}"

# Tests: CI's test job builds this stage on its own (target: test), so pytest
# runs with the image's ffmpeg, which comes from the layer cache instead of an
# apt-get download on every run. The image itself is built from the last stage
# and never contains the tests or the dev tools.
FROM app AS test
COPY tests ./tests
RUN pip install ".[dev]" \
 && pytest -q -p no:cacheprovider

# The image that ships.
FROM app

# Set by CI to the image tag (sha-xxxxxxx or vX.Y.Z); /healthz reports it so the
# deploy job can tell the new image is the one answering.
ARG VERSION=dev
ENV FLIPSTREAM_VERSION=${VERSION}

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "flipstream.app:app", "--host", "0.0.0.0", "--port", "8000"]
