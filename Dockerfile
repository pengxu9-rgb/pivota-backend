# Pivota backend (FastAPI) — Cloud Run image.
# Railway ran this with Railpack + `uvicorn main:app --host 0.0.0.0 --port $PORT` (python-3.11, runtime.txt);
# this reproduces that on a pinned slim base. Cloud Run injects PORT=8080.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# build deps only where wheels are missing; libpq for psycopg2 fallbacks; curl for healthcheck/debug
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# drop the compiler layer from the runtime image
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=base /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=base /usr/local/bin /usr/local/bin
COPY --chown=app:app . .
USER app
EXPOSE 8080
# single uvicorn process: the `databases` pool is per-process and the app runs in-process
# schedulers (apscheduler), so multiple workers would multiply both. Scale with Cloud Run instances.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive ${UVICORN_TIMEOUT_KEEP_ALIVE:-75} --proxy-headers --forwarded-allow-ips='*'"]
