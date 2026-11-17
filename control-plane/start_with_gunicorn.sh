#!/bin/bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# Production start of the Brutor Agent Control control plane with Gunicorn.
#
#   ./start_with_gunicorn.sh community     the Community Edition
#   ./start_with_gunicorn.sh enterprise    the Enterprise Edition
#
# The edition is the argument, and it is required: nothing is guessed and
# nothing is read from the environment: the edition is the application
# factory that is started.
#
# The control plane is a low-traffic admin API; all runtime traffic goes
# through the gateway. Workers, bind address and timeouts are read by
# `gunicorn.conf.py` from the environment (BRUTOR_CONTROL_PLANE_LISTEN_ADDR, GUNICORN_WORKERS).

set -e

EDITION="$1"
case "$EDITION" in
    community)
        FACTORY="app.app:create_app"
        PYTHONPATH_ARGS=()
        ;;
    enterprise)
        FACTORY="brutor_enterprise.app:create_app"
        # In a source tree the Enterprise package is not installed: it is
        # in ./enterprise.
        PYTHONPATH_ARGS=(--pythonpath enterprise)
        if [ ! -d enterprise/brutor_enterprise ]; then
            echo "The Enterprise Edition's package is not in this tree (./enterprise/brutor_enterprise)." >&2
            exit 2
        fi
        ;;
    *)
        echo "usage: $0 community|enterprise" >&2
        exit 2
        ;;
esac

DEBUG=${DEBUG:-0}

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Check if gunicorn is installed
if ! python -c "import gunicorn" 2>/dev/null; then
    echo -e "${RED}Gunicorn is not installed. Install the package: pip install -e .${NC}"
    exit 1
fi

# Load .env file if present
if [ -f .env ]; then
    while IFS='=' read -r key value || [ -n "$key" ]; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        [[ "$key" =~ ^[[:space:]]*$ ]] && continue
        value=$(echo "$value" | sed 's/[[:space:]]*#.*$//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
        key=$(echo "$key" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
        [[ -z "$key" ]] && continue
        export "$key=$value"
        if [ "$DEBUG" = "1" ]; then
            if [[ "$key" =~ (SECRET|PASSWORD|KEY) ]]; then
                echo "  $key=***"
            else
                echo "  $key=$value"
            fi
        fi
    done < .env
else
    echo -e "${YELLOW}.env file not found, using environment${NC}"
fi

# Check required environment variables
REQUIRED_VARS=("JWT_SECRET" "DATABASE_URL" "ENCRYPTION_KEY")
MISSING_VARS=()

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        MISSING_VARS+=("$var")
    fi
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo -e "${RED}Missing required env vars: ${MISSING_VARS[*]}${NC}"
    exit 1
fi

if [ ! -f gunicorn.conf.py ]; then
    echo -e "${RED}gunicorn.conf.py not found: run this script from the repository root${NC}"
    exit 1
fi

echo -e "${GREEN}Starting Brutor Agent Control control plane, ${EDITION} edition (Gunicorn)${NC}"
echo "  ${GUNICORN_WORKERS:-2} workers on ${BRUTOR_CONTROL_PLANE_LISTEN_ADDR:-0.0.0.0:5050}"

exec gunicorn "$FACTORY" "${PYTHONPATH_ARGS[@]}" --config gunicorn.conf.py
