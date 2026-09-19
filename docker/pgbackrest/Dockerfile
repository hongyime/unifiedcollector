FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pgbackrest \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/pgbackrest /var/lib/pgbackrest /etc/pgbackrest \
    && groupadd -g 999 postgres 2>/dev/null || true \
    && useradd -u 999 -g 999 -M -s /bin/sh postgres 2>/dev/null || true \
    && chown -R postgres:postgres /var/log/pgbackrest /var/lib/pgbackrest

USER postgres
WORKDIR /

# Idle by default — the sidecar just holds pgbackrest available for
# docker-exec-driven backup / restore / verify commands. Long-lived so
# the container health endpoint has something to inspect. A cron/scheduler
# handler kicks the actual `pgbackrest backup` on cadence.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["pgbackrest version && exec sleep infinity"]
