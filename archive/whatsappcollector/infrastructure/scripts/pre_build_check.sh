#!/bin/bash
# Pre-build validation script to catch common errors before Docker build

set -e

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# Navigate to workspace root (2 levels up from infrastructure/scripts/)
WORKSPACE_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"

echo "=========================================="
echo "Pre-Build Validation Script"
echo "=========================================="
echo ""
echo "Workspace root: $WORKSPACE_ROOT"
echo ""

# Change to workspace root
cd "$WORKSPACE_ROOT"

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

ERRORS=0

# Check if Python is available
echo "Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 is not installed or not in PATH${NC}"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}✅ Python 3 found${NC}"
fi

# Check if pip is available
echo "Checking pip installation..."
if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
    echo -e "${RED}❌ pip is not installed or not in PATH${NC}"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}✅ pip found${NC}"
fi

# Validate processor-py requirements
echo ""
echo "Validating processor-py requirements.txt..."
if [ -f "services/processor-py/requirements.txt" ]; then
    if command -v python3 &> /dev/null; then
        python3 services/processor-py/scripts/validate_requirements.py services/processor-py/requirements.txt
        if [ $? -ne 0 ]; then
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo -e "${YELLOW}⚠️  Skipping validation (Python not available)${NC}"
    fi
else
    echo -e "${RED}❌ requirements.txt not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

# Check Docker
echo ""
echo "Checking Docker installation..."
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker is not installed or not in PATH${NC}"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}✅ Docker found${NC}"
    docker --version
fi

# Check Docker Compose
echo ""
echo "Checking Docker Compose..."
if ! docker compose version &> /dev/null; then
    echo -e "${RED}❌ Docker Compose is not available${NC}"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}✅ Docker Compose found${NC}"
    docker compose version
fi

# Check for common file issues
echo ""
echo "Checking for common issues..."

# Check for tabs in YAML files
if command -v grep &> /dev/null; then
    if grep -P '\t' docker-compose*.yml 2>/dev/null; then
        echo -e "${YELLOW}⚠️  Warning: Tabs found in docker-compose files (should use spaces)${NC}"
    fi
fi

# Check for secret rotation hygiene
echo ""
echo "Checking secrets rotation hygiene..."

if [ ! -f ".env" ]; then
    echo -e "${RED}❌ .env not found. Copy from .env.template and set rotated secrets.${NC}"
    ERRORS=$((ERRORS + 1))
else
    get_env_value() {
        local key="$1"
        grep -E "^${key}=" .env | tail -n 1 | cut -d'=' -f2- | tr -d '\r'
    }

    is_weak_secret() {
        local value="$1"
        case "$value" in
            ""|"password"|"changeme"|"guest"|"wac_pass"|"wac_redis_pass"|"whatsappcollector_cookie"|"091128"|"CHANGE_ME_"*) return 0 ;;
            *) return 1 ;;
        esac
    }

    POSTGRES_PASSWORD_VAL="$(get_env_value POSTGRES_PASSWORD)"
    REDIS_PASSWORD_VAL="$(get_env_value REDIS_PASSWORD)"
    RABBITMQ_PASSWORD_VAL="$(get_env_value RABBITMQ_PASSWORD)"
    RABBITMQ_COOKIE_VAL="$(get_env_value RABBITMQ_ERLANG_COOKIE)"
    MEDIA_BRIDGE_SECRET_VAL="$(get_env_value MEDIA_BRIDGE_SECRET)"

    for pair in \
        "POSTGRES_PASSWORD:$POSTGRES_PASSWORD_VAL" \
        "REDIS_PASSWORD:$REDIS_PASSWORD_VAL" \
        "RABBITMQ_PASSWORD:$RABBITMQ_PASSWORD_VAL" \
        "RABBITMQ_ERLANG_COOKIE:$RABBITMQ_COOKIE_VAL"; do
        KEY="${pair%%:*}"
        VAL="${pair#*:}"
        if is_weak_secret "$VAL"; then
            echo -e "${RED}❌ $KEY is unset or uses a weak/default value. Rotate it in .env.${NC}"
            ERRORS=$((ERRORS + 1))
        fi
    done

    if [ -z "$MEDIA_BRIDGE_SECRET_VAL" ] || [ ${#MEDIA_BRIDGE_SECRET_VAL} -lt 32 ] || is_weak_secret "$MEDIA_BRIDGE_SECRET_VAL"; then
        echo -e "${RED}❌ MEDIA_BRIDGE_SECRET must be rotated and at least 32 characters.${NC}"
        ERRORS=$((ERRORS + 1))
    else
        echo -e "${GREEN}✅ MEDIA_BRIDGE_SECRET length and rotation check passed${NC}"
    fi
fi

# Verify historical secret-bearing env blobs are purged
echo ""
echo "Checking git history for .env/.env.example blobs..."
if command -v git &> /dev/null; then
    LOCAL_ENV_BLOB_COUNT=$(git rev-list HEAD -- .env .env.example | wc -l | tr -d ' ')
    if [ "$LOCAL_ENV_BLOB_COUNT" -gt 0 ]; then
        echo -e "${RED}❌ Found $LOCAL_ENV_BLOB_COUNT historical env blob commit(s) on current branch. Run secret-history purge before release.${NC}"
        ERRORS=$((ERRORS + 1))
    else
        echo -e "${GREEN}✅ No historical .env/.env.example blobs found on current branch history${NC}"
    fi

    REMOTE_REF_COUNT=$(git for-each-ref refs/remotes --format='%(refname)' | wc -l | tr -d ' ')
    if [ "$REMOTE_REF_COUNT" -gt 0 ]; then
        REMOTE_ENV_BLOB_COUNT=$(git rev-list --remotes -- .env .env.example 2>/dev/null | wc -l | tr -d ' ')
        if [ "$REMOTE_ENV_BLOB_COUNT" -gt 0 ]; then
            echo -e "${YELLOW}⚠️  Remote-tracking refs still contain $REMOTE_ENV_BLOB_COUNT env-blob commit(s). Coordinate remote history cleanup separately.${NC}"
        fi
    fi
else
    echo -e "${YELLOW}⚠️  git not found; skipping history hygiene check${NC}"
fi

# Summary
echo ""
echo "=========================================="
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}✅ All pre-build checks passed!${NC}"
    echo "You can now safely run: docker compose build"
    exit 0
else
    echo -e "${RED}❌ Found $ERRORS error(s). Please fix them before building.${NC}"
    exit 1
fi
