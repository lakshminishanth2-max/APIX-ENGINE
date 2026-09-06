# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# APIx Data & Statistical Core
#
# Two targets share one dependency layer:
#   `runtime` - Django/gunicorn web tier and the cleaning/statistics workers.
#               No browser, ~250 MB.
#   `browser` - the collection worker only.  Adds Chromium and its system
#               libraries (~450 MB more), which the web tier must never carry.
#
# Build:
#   docker build --target runtime -t apix-core:web .
#   docker build --target browser -t apix-core:collector .
# ---------------------------------------------------------------------------

# --------------------------------------------------------------------------- #
# Stage 1: dependency layer
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq for psycopg, build-essential only while wheels compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# --------------------------------------------------------------------------- #
# Stage 2: runtime (web, cleaning worker, statistics worker, beat)
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user.  Nothing in the image needs root.
RUN groupadd --gid 1000 apix \
    && useradd --uid 1000 --gid apix --create-home --shell /bin/bash apix

WORKDIR /app

COPY --from=deps /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=apix:apix . .

RUN mkdir -p /app/staticfiles /app/media \
    && chown -R apix:apix /app/staticfiles /app/media

USER apix

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--threads", "2", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]


# --------------------------------------------------------------------------- #
# Stage 3: browser (collection worker only)
# --------------------------------------------------------------------------- #
FROM runtime AS browser

USER root

# Playwright's own dependency installer keeps the Chromium system library list
# correct across upgrades - pinning it by hand rots quickly.
RUN playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

USER apix

# Browsers live in the user's home so the unprivileged worker can read them.
ENV PLAYWRIGHT_BROWSERS_PATH=/home/apix/.cache/ms-playwright
RUN playwright install chromium

# Chromium needs more than the default 64 MB of /dev/shm; compose sets shm_size.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD celery -A config inspect ping -d "celery@$HOSTNAME" || exit 1

CMD ["celery", "-A", "config", "worker", \
     "--queues", "collection", \
     "--concurrency", "2", \
     "--max-tasks-per-child", "24", \
     "--loglevel", "INFO"]
