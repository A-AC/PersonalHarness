FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HARNESS_DATA_DIR=/data \
    HARNESS_BIND_HOST=0.0.0.0 \
    HARNESS_BIND_PORT=8080

WORKDIR /app

COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY harness ./harness
COPY examples ./examples
COPY pyproject.toml ./

RUN useradd --create-home --uid 10001 harness \
    && mkdir -p /data \
    && chown -R harness:harness /app /data

USER harness

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

CMD ["python", "-m", "harness", "serve"]
