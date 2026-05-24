# Stage 1 — build React frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY services/webdashboard/frontend/package.json ./
RUN npm install
COPY services/webdashboard/frontend/ ./
RUN npm run build

# Stage 2 — FastAPI backend
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY services/webdashboard/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY services/webdashboard/backend/ ./
COPY --from=frontend-builder /app/frontend/dist ./static/
EXPOSE 8500
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500"]
