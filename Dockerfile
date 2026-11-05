FROM python:3.12-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    GARMIN_DB_PATH=/data/activities.sqlite3 \
    GARMIN_ACTIVITIES_PATH=/data/activities \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home app \
    && mkdir -p /data/activities /app/analysis \
    && chown -R app:app /data /app/analysis

COPY app.py ./
COPY web/ ./web/

USER app
EXPOSE 8000
ENTRYPOINT ["python", "app.py"]
CMD ["serve", "--host", "0.0.0.0"]
