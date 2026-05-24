#!/usr/bin/env bash
# Startup wrapper — auto-resolves port conflicts then launches docker compose.
#
# Usage:
#   ./start.sh                  # production stack
#   ./start.sh --dev            # include docker-compose.dev.yml overlay
#   ./start.sh --build          # force rebuild images
#   ./start.sh --dev --build    # both
#   ./start.sh down             # tear down the stack
#   ./start.sh logs [service]   # tail logs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse args ────────────────────────────────────────────────────────────────
DEV=0
BUILD_FLAG=""
COMPOSE_CMD="up -d"

for arg in "$@"; do
    case "$arg" in
        --dev)   DEV=1 ;;
        --build) BUILD_FLAG="--build" ;;
        down)    COMPOSE_CMD="down" ;;
        logs)    shift; COMPOSE_CMD="logs -f ${1:-}" ; break ;;
    esac
done

# ── Port conflict resolution ──────────────────────────────────────────────────
echo ""
echo "▶  Checking ports..."
bash infrastructure/scripts/check_ports.sh
echo ""

# ── Build compose file list ───────────────────────────────────────────────────
COMPOSE_FILES="-f docker-compose.yml"
[[ $DEV -eq 1 ]] && COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.dev.yml"

# ── Merge env files: .env base + .env.ports overrides ────────────────────────
ENV_FILES="--env-file .env"
[[ -f .env.ports ]] && ENV_FILES="$ENV_FILES --env-file .env.ports"

# ── Launch ────────────────────────────────────────────────────────────────────
echo "▶  Running: docker compose $COMPOSE_FILES $ENV_FILES $COMPOSE_CMD $BUILD_FLAG"
echo ""
# shellcheck disable=SC2086
docker compose $COMPOSE_FILES $ENV_FILES $COMPOSE_CMD $BUILD_FLAG
