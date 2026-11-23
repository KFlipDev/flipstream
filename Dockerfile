# yt2sc web image. DEV-44 adds the worker (yt-dlp, ffmpeg) to this image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 --no-create-home app

# Set by CI to the image tag (sha-xxxxxxx or vX.Y.Z); /healthz reports it so the
# deploy job can tell the new image is the one answering.
ARG VERSION=dev
ENV YT2SC_VERSION=${VERSION}

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "yt2sc.app:app", "--host", "0.0.0.0", "--port", "8000"]
