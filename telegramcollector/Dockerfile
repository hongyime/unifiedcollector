# Multi-stage Dockerfile for Telegram Face Recognition System
# Optimized for size and build caching

# ============================================
# Stage 1: Builder
# ============================================
FROM python:3.14-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavfilter-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

# Install Python dependencies
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

# ============================================
# Stage 2: Runtime
# ============================================
FROM python:3.14-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    curl \
    ntpsec-ntpdate \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder and install
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache /wheels/*

# Download InsightFace model at build time with retry logic
# The model is ~330MB from GitHub Releases and can fail on slow/unreliable networks
RUN mkdir -p /root/.insightface/models && \
    for i in 1 2 3 4 5; do \
        echo "Attempt $i: Downloading buffalo_l model..."; \
        curl -fSL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 600 \
            -o /root/.insightface/models/buffalo_l.zip \
            "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip" && break; \
        echo "Download attempt $i failed, retrying in 10s..."; \
        sleep 10; \
    done && \
    cd /root/.insightface/models && \
    python -c "import zipfile; zipfile.ZipFile('buffalo_l.zip').extractall()" && \
    rm buffalo_l.zip && \
    python -c "from insightface.app import FaceAnalysis; app = FaceAnalysis(name='buffalo_l'); app.prepare(ctx_id=-1)" && \
    echo 'InsightFace model ready.'

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p sessions logs

# Normalize shell script line endings and permissions for Windows/WSL checkouts
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Set environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Telegram Client Feature Flags (Production-safe rollout - Phase 1: disabled)
ENV ENABLE_MTPROTO_RESET=false
ENV ENABLE_SESSION_LOCK=true
ENV MTPROTO_RESET_NEW_SESSIONS_ONLY=true

# Entrypoint handles clock sync before starting the app
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]

# Default command
CMD ["python", "worker.py"]

# ============================================
# Health check
# ============================================
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import asyncio, sys; from shared.database import check_db_health; sys.exit(0 if asyncio.run(check_db_health()) else 1)"
