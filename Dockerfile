# The sha is baked in at build time:
#   docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t ... .
ARG GIT_SHA=unknown

FROM python:3.12-slim AS base

# Re-declare inside the stage: an ARG before the first FROM is not
# visible in instructions after it.
ARG GIT_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HARNESS_DATA_DIR=/data \
    HARNESS_HOST=0.0.0.0 \
    HARNESS_PORT=8080 \
    HARNESS_GIT_SHA=${GIT_SHA}

LABEL org.opencontainers.image.version="2.0.0"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY harness ./harness
COPY examples ./examples
RUN pip install --no-cache-dir .

# Deterministic test suite inside the image ("green on Linux" before
# promotion): docker build --target test . && docker run <image>
# Tests read eval/scenarios, Dockerfile, and docker-compose.stage.yml from
# the repository root, so those are copied in as well.
FROM base AS test

COPY tests ./tests
COPY scripts ./scripts
COPY eval ./eval
COPY Dockerfile docker-compose.stage.yml .
RUN pip install --no-cache-dir pytest
CMD ["python", "-m", "pytest", "tests", "-o", "addopts=", "-q"]

# Default (last) stage: the runtime image.
FROM base AS runtime

RUN useradd --create-home --uid 10001 harness \
    && mkdir -p /data \
    && chown -R harness:harness /app /data

USER harness

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

CMD ["python", "-m", "harness", "serve"]
