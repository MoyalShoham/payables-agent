# syntax=docker/dockerfile:1
FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.19
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src src
COPY alembic alembic
COPY alembic.ini ./
COPY eval eval
RUN uv sync --frozen --no-dev && useradd --system --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "--factory", "payables.app:make", "--host", "0.0.0.0", "--port", "8000"]
