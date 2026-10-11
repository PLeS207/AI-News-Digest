FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

# Non-root user; data dirs are created with the right owner so named volumes inherit it.
RUN useradd --uid 10001 --create-home appuser \
    && mkdir -p /data/editor /data/traces \
    && chown -R appuser:appuser /data

COPY --chown=appuser:appuser pyproject.toml ./
COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser config ./config
COPY --chown=appuser:appuser dashboard ./dashboard
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser tests ./tests

USER appuser

CMD ["python", "-m", "ai_news_digest.telegram_bot"]
