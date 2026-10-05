# syntax=docker/dockerfile:1
# Backend image: FastAPI API + data pipeline + the dashboard it serves at /.
# The build context is the repository root (what is sent is limited by the root .dockerignore):
#   docker compose build backend
#   docker build -f backend/Dockerfile -t dodge-city-infra-backend .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Unprivileged runtime user with a fixed uid/gid.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

# Dependencies first: this layer is rebuilt only when requirements.txt changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application files, the least frequently changed first. data/raw/ is the committed GIS cache, so neither the
# pipeline nor the API needs the network.
COPY data/raw/ data/raw/
COPY dashboard/ dashboard/
COPY sql/ sql/
COPY pyproject.toml README.md run_pipeline.py ./
COPY scripts/ scripts/
COPY pipeline/ pipeline/
COPY backend/ backend/

# The editable install is a path entry for /app, so pipeline.config.ROOT resolves to /app (data/, sql/, dashboard/).
# data/processed/ is written by the pipeline at run time (AUTO_SEED) and is the only directory the app user owns.
RUN pip install -e . --no-deps \
    && mkdir -p data/processed \
    && chown app:app data/processed

USER app
EXPOSE 8000

# Wait for the database, apply migrations, seed when empty (AUTO_SEED), then exec uvicorn on API_HOST:API_PORT.
ENTRYPOINT ["python", "-m", "backend.entrypoint"]
