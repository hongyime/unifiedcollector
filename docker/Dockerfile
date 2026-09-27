FROM node:22-slim AS frontend-dependencies
WORKDIR /app/frontend
COPY src/dashboard/frontend/package*.json ./
RUN npm ci
FROM frontend-dependencies AS frontend-dev
COPY --chown=node:node src/dashboard/frontend/ /app/frontend/
RUN chown -R node:node /app/frontend
USER node
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
FROM frontend-dependencies AS frontend-builder
COPY src/dashboard/frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS dependencies
ENV VIRTUAL_ENV=/opt/collector-venv \
    PATH="/opt/collector-venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake libolm-dev git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/collector-venv
COPY requirements.txt requirements.lock ./
RUN sed '/^[Pp][Yy][Tt][Ee][Ss][Tt]/d' requirements.lock > /tmp/runtime-requirements.txt \
    && pip install --no-cache-dir -r /tmp/runtime-requirements.txt \
    && pip install --no-cache-dir phonenumbers==9.0.38 \
    && python -m playwright install chromium

FROM python:3.12-slim-bookworm AS runtime-dependencies
ENV VIRTUAL_ENV=/opt/collector-venv \
    PATH="/opt/collector-venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget curl libolm3 sqlite3 ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /opt/collector-venv /opt/collector-venv
COPY --from=dependencies /ms-playwright /ms-playwright
RUN python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -u 1001 -m collector \
    && mkdir -p /app/data /app/sessions /app/logs /home/collector/.cache \
    && chown -R collector:collector /app /home/collector

FROM runtime-dependencies AS dev
RUN pip install --no-cache-dir watchfiles==1.3.0
COPY --chown=collector:collector src/ /app/src/
COPY --chown=collector:collector config/ /app/config/
USER collector
CMD ["uvicorn", "src.dashboard.api:app", "--host", "0.0.0.0", "--port", "8700", "--reload", "--reload-dir", "/app/src"]

FROM runtime-dependencies AS dashboard
COPY src/ src/
COPY config/ config/
COPY --from=frontend-builder /app/frontend/dist/ src/dashboard/frontend/dist/
USER collector
CMD ["uvicorn", "src.dashboard.api:app", "--host", "0.0.0.0", "--port", "8700"]

FROM dashboard AS production
CMD ["python", "-m", "src.main"]
