FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    numpy==1.26.4 \
    Pillow==12.1.1 \
    opencv-python-headless==4.9.0.80 \
    onnxruntime==1.19.2 \
    insightface==0.7.3 \
    telethon==1.36.0 \
    psycopg[binary]==3.1.18 \
    psycopg-pool==3.2.1 \
    python-dotenv==1.0.1 \
    redis==5.0.1 \
    pgvector==0.2.5 \
    streamlit==1.54.0 \
    pandas==2.2.1 \
    pydantic-settings==2.2.1 \
    prometheus_client==0.20.0 \
    python-json-logger==2.0.7 \
    asyncpg==0.29.0 \
    psycopg2-binary==2.9.9

COPY services/face_recognition/ services/face_recognition/
COPY shared/ shared/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

EXPOSE 8502

CMD ["python", "-m", "services.face_recognition.main"]
