FROM node:20-alpine AS dependencies
WORKDIR /app
RUN apk add --no-cache git python3 make g++
COPY package.json package-lock.json ./
RUN npm ci --network-timeout=1000000

FROM dependencies AS dev
COPY --chown=node:node src/ /app/src/
COPY --chown=node:node tsconfig.json /app/tsconfig.json
RUN mkdir -p /app/build /app/auth_info /data/media \
    && chown -R node:node /app /data/media
USER node
ENV TSC_WATCHFILE=PriorityPollingInterval \
    TSC_WATCHDIRECTORY=RecursiveDirectoryUsingFsWatchFile
CMD ["sh", "-ec", "npm run build\nnode node_modules/typescript/bin/tsc --watch --preserveWatchOutput &\nexec node --watch build/index.js"]

FROM dependencies AS builder
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

FROM dependencies AS production-dependencies
RUN npm prune --omit=dev

FROM node:20-alpine AS production
WORKDIR /app
RUN addgroup -S wac && adduser -S wac -G wac \
    && apk add --no-cache git wget
COPY package.json package-lock.json ./
COPY --from=production-dependencies /app/node_modules ./node_modules
COPY --from=builder /app/build ./build
RUN mkdir -p /app/auth_info /data/media \
    && chown -R wac:wac /app/auth_info /data/media
USER wac
EXPOSE 3001
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3001/livez || exit 1
CMD ["node", "build/index.js"]
