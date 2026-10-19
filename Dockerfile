FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

FROM base AS runtime
COPY requirements.txt ./requirements.txt
RUN python -m pip install -r requirements.txt && python -m pip check

RUN useradd --create-home --uid 10001 app && chown app:app /app
COPY --chown=app:app app ./app
COPY --chown=app:app jobs ./jobs
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini pyproject.toml ./

USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS development
USER root
COPY requirements-dev.txt ./requirements-dev.txt
RUN python -m pip install -r requirements-dev.txt && python -m pip check
COPY --chown=app:app tests ./tests
USER app
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# A plain `docker build .` produces the runtime image without test tools or reload.
FROM runtime AS production
