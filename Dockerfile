FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libmagic1 is a native dependency of python-magic (pulled in transitively by
# python-trueconf-bot for MIME type detection in URLInputFile). The slim image
# does not ship with it, so install explicitly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata first so the dependency layer is reused across
# rebuilds as long as pyproject.toml hasn't changed.
COPY pyproject.toml ./
COPY trueconf_webhook_bot/__init__.py ./trueconf_webhook_bot/__init__.py
# --pre allows installing the python-trueconf-bot 1.2.0bX beta, which is
# required for TrueConf Server 5.5.3+.
RUN pip install --pre .

# Full source tree.
COPY trueconf_webhook_bot/ ./trueconf_webhook_bot/

# Unprivileged user; /app/data is the mount point for the persistent JSON store.
RUN useradd --system --create-home --shell /usr/sbin/nologin webhookbot \
    && mkdir -p /app/data \
    && chown -R webhookbot:webhookbot /app
USER webhookbot

ENV WEBHOOK_HTTP_HOST=0.0.0.0 \
    WEBHOOK_HTTP_PORT=8080 \
    WEBHOOK_STORAGE_PATH=/app/data/webhooks.json

EXPOSE 8080

VOLUME ["/app/data"]

ENTRYPOINT ["python", "-m", "trueconf_webhook_bot"]
