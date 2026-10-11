# UnifiedCollector Network Architecture

## Overview

UnifiedCollector and UnifiedAnalyzer run as separate Docker Compose stacks that share a Docker network for cross-stack data access.

## Networks

| Network | Purpose | Created By |
|---------|---------|------------|
| `unifiedcollector_default` | Collector internal + shared with analyzer | unifiedcollector compose |
| `unifiedanalyzer_default` | Analyzer internal | unifiedanalyzer compose |

**Important:** The analyzer intentionally joins the collector's network to read collector data. This is by design.

## Container Hostnames

### Critical: Use Full Hostnames, Not Short Names

**The 2026-10-11 DNS Incident:** Both stacks defined a service named `postgres`. Docker's embedded DNS round-robined the hostname `postgres` between:
- `172.20.0.2` = `unifiedcollector_postgres` (correct for collector)
- `172.20.0.3` = `unifiedanalyzer_postgres` (wrong for collector)

The collector scheduler intermittently connected to the analyzer's database, getting "database unifiedcollector does not exist" errors.

**Fix Applied:** All connection strings now use full container names:
- Collector: `@unifiedcollector_postgres:5432/unifiedcollector`
- Analyzer: `@unifiedanalyzer_postgres:5432/unifiedanalyzer`

**Rule:** NEVER use the short hostname `postgres` in connection strings. Always use the full container name.

## Container Map

### UnifiedCollector Stack

| Container | Hostname | Network | Purpose |
|-----------|----------|---------|---------|
| unifiedcollector_postgres | unifiedcollector_postgres | unifiedcollector_default | Main database (146 tables) |
| unifiedcollector_redis | unifiedcollector_redis | unifiedcollector_default | Cache/queues |
| unifiedcollector_rabbitmq | unifiedcollector_rabbitmq | unifiedcollector_default | Message broker |
| unifiedcollector_scheduler | unifiedcollector_scheduler | unifiedcollector_default | Job scheduler |
| unifiedcollector_dashboard | unifiedcollector_dashboard | unifiedcollector_default | Web UI (:8700) |
| unifiedcollector_browser_cookie_vault | unifiedcollector_browser_cookie_vault | unifiedcollector_default | Chrome cookie sync |
| unifiedcollector_collector_* | various | unifiedcollector_default | Individual collectors |

### UnifiedAnalyzer Stack

| Container | Hostname | Network | Purpose |
|-----------|----------|---------|---------|
| unifiedanalyzer_postgres | unifiedanalyzer_postgres | unifiedanalyzer_default + unifiedcollector_default | Analysis DB (425 tables) |
| unifiedanalyzer_scheduler | unifiedanalyzer_scheduler | unifiedanalyzer_default | Pipeline scheduler |
| unifiedanalyzer_analyzer | unifiedanalyzer_analyzer | unifiedanalyzer_default | Analysis workers |
| unifiedanalyzer_face_worker | unifiedanalyzer_face_worker | unifiedanalyzer_default | Face recognition |

## External Connections

| From | To | How |
|------|----|-----|
| browser_cookie_vault (container) | Chrome (host) | `host.docker.internal:9222` via CDP |
| Dashboard (container) | User browser | Port 8700 published to host |
| Collectors (containers) | Seagate drive | `/media/seagate-expansion` bind mount |

## Troubleshooting

| Symptom | Likely Cause | Check |
|---------|--------------|-------|
| "database does not exist" | Wrong postgres via DNS | `docker exec <container> getent hosts postgres` — should return ONE IP |
| Connection refused to postgres | Container not on network | `docker network inspect unifiedcollector_default` |
| CDP timeout | Chrome not running | `curl http://localhost:9222/json/version` on host |
