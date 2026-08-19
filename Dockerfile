FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LEADORBYT_DB_PATH=/data/leadorbyt.db \
    LEADORBYT_OUTPUT_DIR=/data/leads_output

WORKDIR /app

# playwright/patchright's install-deps needs apt + a non-interactive frontend
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY leadorbyt ./leadorbyt

RUN pip install . \
    && python -m playwright install --with-deps chromium \
    && python -m patchright install chromium \
    && python -m patchright install-deps chromium

RUN mkdir -p /data/leads_output

EXPOSE 8000

CMD ["leadorbyt"]
