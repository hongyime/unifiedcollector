# UnifiedCollector network architecture

UnifiedCollector and UnifiedAnalyzer run as separate Compose stacks. Where the
Analyzer joins the Collector network, use explicit database hostnames so the
two `postgres` services cannot be confused by Docker's embedded DNS.

## Networks and database hosts

- Collector network: `unifiedcollector_default`
- Analyzer network: `unifiedanalyzer_default`
- Collector database: `unifiedcollector_postgres:5432/unifiedcollector`
- Analyzer database: `unifiedanalyzer_postgres:5432/unifiedanalyzer`

Do not use the short hostname `postgres` in a connection string when both
stacks share a network. A 2026-10-11 DNS incident showed it could resolve to
either stack's database. The repository Compose configuration uses the explicit
Collector container name.

## Service endpoints

- Dashboard: host ports 8700 and 8001.
- Browser ingest bridge: port 8765.
- Browser cookie vault health endpoint: port 8790.
- Host Chrome CDP: `CHROME_CDP_URL` defaults to
  `http://host.docker.internal:9336` in Compose.
- Beeper Desktop API: `host.docker.internal:23373` by default.

Media and vault bind mounts use `UNIFIEDCOLLECTOR_HOST_ROOT`. Set it in the
project `.env` to a directory visible to the Docker daemon on that host.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `database does not exist` | Connection points at the other stack | Check the full database hostname and database name in the container environment. |
| Connection refused to Postgres | Service is unavailable or network membership is missing | `docker network inspect unifiedcollector_default` |
| CDP timeout | Chrome is stopped, using another port, or unreachable from Docker | `curl http://localhost:9336/json/version` on the host and check `CHROME_CDP_URL`. |
