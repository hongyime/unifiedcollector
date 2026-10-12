FROM postgres:18-alpine

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/pg-dump-venv \
    && /opt/pg-dump-venv/bin/pip install --no-cache-dir asyncpg==0.31.0

WORKDIR /app
ENTRYPOINT ["/opt/pg-dump-venv/bin/python", "/app/pg_dump_backup.py"]
