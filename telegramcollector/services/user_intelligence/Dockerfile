FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    psycopg[binary]==3.1.18 \
    psycopg-pool==3.2.1 \
    python-dotenv==1.0.1 \
    redis==5.0.1 \
    streamlit==1.54.0 \
    pandas==2.2.1 \
    networkx==3.2.1 \
    matplotlib==3.8.3 \
    pydantic-settings==2.2.1 \
    prometheus_client==0.20.0 \
    python-json-logger==2.0.7 \
    asyncpg==0.29.0 \
    psycopg2-binary==2.9.9

COPY shared/ shared/
COPY services/user_intelligence/ services/user_intelligence/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

EXPOSE 8503

CMD ["python", "-m", "services.user_intelligence.main"]
