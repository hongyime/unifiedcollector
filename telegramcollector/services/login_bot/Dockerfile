FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    telethon==1.36.0 \
    psycopg[binary]==3.1.18 \
    psycopg-pool==3.2.1 \
    python-dotenv==1.0.1 \
    pydantic-settings==2.2.1 \
    prometheus_client==0.20.0 \
    python-json-logger==2.0.7 \
    asyncpg==0.29.0 \
    psycopg2-binary==2.9.9

COPY shared/ shared/
COPY services/login_bot/ services/login_bot/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "services.login_bot.main"]
