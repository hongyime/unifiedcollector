# Build stage
FROM node:20-alpine AS builder
WORKDIR /app

# Install OS dependencies first (cached separately)
RUN apk add --no-cache git python3 make g++

# Then handle npm install with high timeout for slow networks
COPY package.json ./
RUN npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm install --network-timeout=1000000

COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# Production stage
FROM node:20-alpine
WORKDIR /app

# Add non-root user
RUN addgroup -S wac && adduser -S wac -G wac

# Reuse same setup for production (without devDeps)
RUN apk add --no-cache git python3 make g++
COPY package.json ./
RUN npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm install --omit=dev --network-timeout=1000000

COPY --from=builder /app/build ./build

# Storage directories
RUN mkdir -p /app/auth_info /data/media && \
    chown -R wac:wac /app/auth_info /data/media

USER wac
EXPOSE 3001

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3001/health || exit 1

CMD ["node", "build/index.js"]
