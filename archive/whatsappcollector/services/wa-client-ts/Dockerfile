# Build stage
FROM node:20-alpine AS builder
WORKDIR /app

# Install OS dependencies first (cached separately)
RUN apk add --no-cache git python3 make g++

# Then handle npm install with high timeout for slow networks.
COPY package.json package-lock.json ./
RUN npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm ci --no-audit --network-timeout=1000000

COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# Production dependencies stage. Native build tools stay out of the final image.
FROM node:20-alpine AS production-deps
WORKDIR /app

RUN apk add --no-cache git python3 make g++
COPY package.json package-lock.json ./
RUN npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm ci --omit=dev --no-audit --network-timeout=1000000 && \
    npm cache clean --force

# Production stage
FROM node:20-alpine
WORKDIR /app

# Add non-root user
RUN addgroup -S wac && adduser -S wac -G wac

COPY package.json ./
COPY --from=production-deps /app/node_modules ./node_modules
COPY --from=builder /app/build ./build

# Storage directories
RUN mkdir -p /app/auth_info /data/media && \
    chown -R wac:wac /app/auth_info /data/media

USER wac
EXPOSE 3001

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3001/health || exit 1

CMD ["node", "build/index.js"]
