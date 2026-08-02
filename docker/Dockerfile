FROM python:3.12-slim

WORKDIR /app

# OS deps for headless Chromium (Playwright Mode β fallback in Instagram collector).
# Kept tight: only chromium is installed, no firefox/webkit.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatk1.0-0 libcups2 libxshmfence1 fonts-liberation \
    ca-certificates wget curl ffmpeg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
# P1-3: install from the lockfile for reproducible builds. requirements.txt is
# kept for human-readable ranges; requirements.lock has exact proven pins.
RUN pip install --no-cache-dir -r requirements.lock

# Install Playwright Chromium only (no firefox/webkit — saves ~400MB).
# install-deps is still useful as a belt-and-braces top-up of any missing libs.
RUN python -m playwright install chromium \
 && python -m playwright install-deps chromium

# sqlite3 CLI for self-healing corrupt SQLite session files (session_repair.py).
# Placed AFTER the heavy pip/playwright layers so those stay cached on rebuild
# (keeps the image/vhdx growth minimal).
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 \
 && rm -rf /var/lib/apt/lists/*

COPY src/ src/

# Bake a baseline copy of the per-source config files. At runtime the compose
# bind-mount (../config/sources:/app/config/sources:ro) overlays these so an
# operator can edit the host files + `docker restart` without a rebuild; the
# baked copy is just a fallback if the mount is ever absent.
COPY config/ config/

CMD ["python", "-m", "src.main"]
