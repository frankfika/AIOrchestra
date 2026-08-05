# M4 OSS-001 — Orchestra single-image Dockerfile.
#
# Two services use this image:
#   * `orchestra-api`  — the FastAPI server (port 8000)
#   * `orchestra-cli`  — ad-hoc CLI runner (no exposed port)
#
# The image runs as a non-root user. PG lives in a sibling container
# in docker-compose.yml; the API connects via DATABASE_URL.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for psycopg + cryptography. Keeps the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Install the package itself.
COPY pyproject.toml /app/pyproject.toml
COPY orchestra /app/orchestra
COPY data /app/data
COPY README.md /app/README.md
COPY LICENSE /app/LICENSE
RUN pip install --no-deps /app

# Non-root user.
RUN useradd -m -u 10001 orchestra && chown -R orchestra:orchestra /app
USER orchestra

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Default to the API. docker-compose overrides the command for the CLI.
CMD ["python", "-m", "uvicorn", "orchestra.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
