FROM python:3.12-slim

WORKDIR /app

# OS deps for headless Chromium (Playwright Mode β fallback in Instagram collector).
# Kept tight: only chromium is installed, no firefox/webkit.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatk1.0-0 libcups2 libxshmfence1 fonts-liberation \
    ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium only (no firefox/webkit — saves ~400MB).
# install-deps is still useful as a belt-and-braces top-up of any missing libs.
RUN python -m playwright install chromium \
 && python -m playwright install-deps chromium

COPY src/ src/

CMD ["python", "-m", "src.main"]
