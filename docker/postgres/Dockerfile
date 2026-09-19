FROM pgvector/pgvector:pg16

# pgBackRest for WAL archiving + full/diff/incr backups. Runs both as the
# archive_command from inside this container and as a standalone binary from
# the sidecar (see docker/pgbackrest/). Repo-modernization plan:
#   docs/plans/backup-modernization-pgbackrest.md
#
# Debian trixie ships pgbackrest 2.51 in the main archive; no third-party
# apt list needed.

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pgbackrest \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/pgbackrest /var/lib/pgbackrest \
    && chown -R postgres:postgres /var/log/pgbackrest /var/lib/pgbackrest

# Sanity check that the binary works in this image.
RUN pgbackrest version

# Keep the upstream entrypoint / cmd — this image is drop-in for pgvector.
