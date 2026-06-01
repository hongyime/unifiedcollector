# Native WhatsApp bridge for unifiedcollector.
# Multi-stage: build TS -> run compiled JS as non-root.

# ---- build stage ----
FROM node:20-alpine AS builder
WORKDIR /app

RUN apk add --no-cache git python3 make g++

COPY package.json ./
RUN npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm install --network-timeout=1000000

COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# ---- production stage ----
FROM node:20-alpine
WORKDIR /app

RUN addgroup -S wac && adduser -S wac -G wac
RUN apk add --no-cache git python3 make g++ wget

COPY package.json ./
RUN npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm install --omit=dev --network-timeout=1000000

COPY --from=builder /app/build ./build

# auth_info holds the Baileys multi-file session (mounted from host sessions/whatsapp)
RUN mkdir -p /app/auth_info /data/media && \
    chown -R wac:wac /app/auth_info /data/media

USER wac
EXPOSE 3001

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3001/health || exit 1

CMD ["node", "build/index.js"]
