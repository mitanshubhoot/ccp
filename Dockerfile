# One image for both services (A-138): `ccp serve` (api) and `ccp worker run` (worker).
FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY ccp ccp
COPY config config
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home ccp && mkdir -p /app/data && chown ccp /app/data
USER ccp
ENV PATH="/app/.venv/bin:$PATH" \
    CCP_DATA_DIR=/app/data

EXPOSE 8000
CMD ["ccp", "serve", "--host", "0.0.0.0", "--port", "8000"]
