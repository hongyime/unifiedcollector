# Stage 1: Build React frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

COPY services/dashboard/frontend/package.json ./
RUN npm install

COPY services/dashboard/frontend/ ./
RUN npm run build


# Stage 2: FastAPI backend + serve built frontend
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/dashboard/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY services/dashboard/backend/ ./
COPY --from=frontend-builder /app/frontend/dist ./static/

EXPOSE 8700
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8700"]
