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

# Stamp the commit INTO the image, so what /health reports is a property of the code rather than a
# claim about it. Until now the only source was the PIVOTA_COMMIT_SHA env var set at deploy time,
# which means any deploy that forgets it leaves the previous value in place: on 2026-08-23 a manual
# `gcloud run deploy` rolled prod onto the eecb983b image while /health kept reporting b13b8a9b,
# and the drift alarm - whose entire job is comparing that value to main - reasoned about the wrong
# commit. A stale label that happens to MATCH main is worse still: the alarm goes green over
# undeployed code.
#
# A file, not an ENV: `gcloud run deploy --set-env-vars/--update-env-vars` can override an image
# ENV, so the guarantee would only hold until someone set that name. Nothing in a Cloud Run deploy
# can rewrite a file inside the image.
#
# Empty when built without the arg (local `docker build`), and the env vars remain the fallback.
ARG COMMIT_SHA=""
RUN printf '%s' "$COMMIT_SHA" > /app/.image_commit_sha \
    && chown app:app /app/.image_commit_sha

USER app
EXPOSE 8080
# single uvicorn process: the `databases` pool is per-process and the app runs in-process
# schedulers (apscheduler), so multiple workers would multiply both. Scale with Cloud Run instances.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive ${UVICORN_TIMEOUT_KEEP_ALIVE:-75} --proxy-headers --forwarded-allow-ips='*'"]
