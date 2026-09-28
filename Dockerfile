FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY ba_flow ./ba_flow
RUN pip install .

# Settings, prompts and templates are read from /app (the working directory). docker-compose mounts
# them read-only from the host so they can be edited without rebuilding the image.
COPY config ./config
COPY templates ./templates

RUN useradd --system --uid 10001 --home-dir /app baflow \
    && mkdir -p /app/data /app/logs \
    && chown -R baflow /app/data /app/logs
USER baflow

EXPOSE 8080
# `serve` = webhook / hybrid mode. For polling-only mode use: CMD ["ba-flow", "run"]
CMD ["ba-flow", "serve"]
