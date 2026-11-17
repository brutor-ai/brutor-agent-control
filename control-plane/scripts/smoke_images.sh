#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Smoke test of the two control plane images.
#
#   scripts/smoke_images.sh --community IMAGE [--enterprise IMAGE]
#   scripts/smoke_images.sh --enterprise IMAGE
#
# Each image is started against a real PostgreSQL and Redis, on a scratch
# database this script creates and drops, and asked what only a running image
# can answer:
#
#   Community   no Enterprise package and no Enterprise-only library is
#               importable; the sources are in the image; the console script
#               runs; a first start without the administrator's password is
#               refused and names the variable; with it (and with no licence
#               key and no JWT_ALGORITHM) the image creates its schema,
#               answers /health, lets the tenant administrator sign in with
#               the configured password, and serves an OpenAPI document with
#               no Enterprise route.
#   Enterprise  the image holds bytecode only; the Enterprise libraries
#               import; the console script runs; it starts, answers /health,
#               lets the system administrator sign in, and its OpenAPI
#               document has the Enterprise routes.
#
# Where PostgreSQL and Redis are (defaults: the throwaway pair of the
# development machine, reached from a container through the host):
#
#   SMOKE_PG_HOST      host.docker.internal
#   SMOKE_PG_PORT      25432
#   SMOKE_PG_USER      brutor
#   SMOKE_PG_PASSWORD  throwaway
#   SMOKE_PG_ADMIN_DB  postgres        (connected to only to create/drop)
#   SMOKE_REDIS_URL    redis://host.docker.internal:26379/15
#
# The scratch databases are named control_smoke_<edition>_<pid>. The script
# never touches a database it did not create. Every secret it needs is
# generated here, lives for the run and is never printed.
# =============================================================================

set -euo pipefail

COMMUNITY_IMAGE=""
ENTERPRISE_IMAGE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --community) COMMUNITY_IMAGE="$2"; shift 2 ;;
        --enterprise) ENTERPRISE_IMAGE="$2"; shift 2 ;;
        *) echo "usage: $0 [--community IMAGE] [--enterprise IMAGE]" >&2; exit 2 ;;
    esac
done
if [ -z "$COMMUNITY_IMAGE" ] && [ -z "$ENTERPRISE_IMAGE" ]; then
    echo "usage: $0 [--community IMAGE] [--enterprise IMAGE]" >&2
    exit 2
fi

PG_HOST="${SMOKE_PG_HOST:-host.docker.internal}"
PG_PORT="${SMOKE_PG_PORT:-25432}"
PG_USER="${SMOKE_PG_USER:-brutor}"
PG_PASSWORD="${SMOKE_PG_PASSWORD:-throwaway}"
PG_ADMIN_DB="${SMOKE_PG_ADMIN_DB:-postgres}"
REDIS_URL="${SMOKE_REDIS_URL:-redis://host.docker.internal:26379/15}"
START_TIMEOUT="${SMOKE_START_TIMEOUT:-180}"

# On Linux a container does not know the host by name unless told.
HOST_ARGS=(--add-host=host.docker.internal:host-gateway)

# What only Enterprise Edition code imports, and its package.
ENTERPRISE_MODULES="brutor_enterprise docker kubernetes qdrant_client aiohttp croniter"
# One route of several Enterprise features (editions.yaml,
# control_plane.enterprise_route_prefixes).
ENTERPRISE_PATHS="/v1/admin/tenants /scim/v2/Users /v1/admin/knowledge-base/collections /v1/admin/users/system/login"

# The application reads DATABASE_URL when it is imported; `--help` connects to nothing.
NO_DATABASE="postgresql://nobody:nothing@127.0.0.1:1/none"

CONTAINERS=()
DATABASES=()   # "image|name"
FAILED=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=1; }

random_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(36))'; }

# Create or drop a scratch database, using the image's own PostgreSQL driver.
database() {  # image verb name
    case "$3" in
        control_smoke_*) ;;
        *) echo "refusing to $2 a database this script did not name: $3" >&2; return 1 ;;
    esac
    docker run --rm "${HOST_ARGS[@]}" \
        -e PGHOST="$PG_HOST" -e PGPORT="$PG_PORT" -e PGUSER="$PG_USER" -e PGPASSWORD="$PG_PASSWORD" \
        -e PGDATABASE="$PG_ADMIN_DB" -e SMOKE_VERB="$2" -e SMOKE_DB="$3" \
        "$1" python -c '
import os, psycopg2
conn = psycopg2.connect("")
conn.autocommit = True
name = os.environ["SMOKE_DB"]
assert name.startswith("control_smoke_") and name.replace("_", "").isalnum(), name
cur = conn.cursor()
if os.environ["SMOKE_VERB"] == "create":
    cur.execute(f"CREATE DATABASE {name}")
else:
    cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
'
}

cleanup() {
    for container in "${CONTAINERS[@]:-}"; do
        [ -n "$container" ] && docker rm -f "$container" >/dev/null 2>&1 || true
    done
    for entry in "${DATABASES[@]:-}"; do
        [ -n "$entry" ] && database "${entry%%|*}" drop "${entry##*|}" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT

in_image() {  # image, then a python one-liner
    docker run --rm "$1" python -c "$2" >/dev/null 2>&1
}

http_code() {  # url [curl args...]
    local url="$1"; shift
    curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$url" || true
}

wait_for_health() {  # container port
    local deadline=$(( $(date +%s) + START_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" != "true" ]; then
            return 1
        fi
        if [ "$(http_code "http://127.0.0.1:$2/health")" = "200" ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# The caller registers the container for clean-up first: this function's
# output is captured, so it runs in a subshell and cannot.
start() {  # image name database, then extra -e arguments; prints the host port
    local image="$1" name="$2" db="$3"; shift 3
    docker run -d --name "$name" "${HOST_ARGS[@]}" -p 127.0.0.1::5050 \
        -e DATABASE_URL="postgresql://$PG_USER:$PG_PASSWORD@$PG_HOST:$PG_PORT/$db" \
        -e REDIS_URL="$REDIS_URL" \
        -e JWT_SECRET="$JWT_SECRET" -e ENCRYPTION_KEY="$ENCRYPTION_KEY" \
        -e GUNICORN_WORKERS=1 \
        "$@" "$image" >/dev/null
    docker port "$name" 5050/tcp | head -1 | sed 's/.*://'
}

sign_in() {  # port path json-body ; succeeds when the answer is 200 with a token
    local body
    body="$(curl -s --max-time 20 -X POST "http://127.0.0.1:$1$2" -H 'Content-Type: application/json' -d "$3" || true)"
    printf '%s' "$body" | python3 -c '
import json, sys
try:
    answer = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
token = answer.get("access_token") or answer.get("token") or (answer.get("data") or {}).get("access_token")
sys.exit(0 if token else 1)
'
}

openapi_paths() {  # port ; prints one path per line
    curl -s --max-time 30 "http://127.0.0.1:$1/openapi.json" | python3 -c '
import json, sys
print("\n".join(sorted(json.load(sys.stdin)["paths"])))
'
}

tables_in() {  # image database ; prints the number of tables in the public schema
    docker run --rm "${HOST_ARGS[@]}" \
        -e PGHOST="$PG_HOST" -e PGPORT="$PG_PORT" -e PGUSER="$PG_USER" -e PGPASSWORD="$PG_PASSWORD" -e PGDATABASE="$2" \
        "$1" python -c '
import psycopg2
cur = psycopg2.connect("").cursor()
cur.execute("select count(*) from information_schema.tables where table_schema = %s", ("public",))
print(cur.fetchone()[0])
'
}

JWT_SECRET="$(random_secret)"
ENCRYPTION_KEY="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
TENANT_ADMIN_PASSWORD="$(random_secret)"
BRUTOR_SYSTEM_ADMIN_PASSWORD="$(random_secret)"

# -----------------------------------------------------------------------------
smoke_community() {
    local image="$1" db="control_smoke_community_$$" name="control-smoke-community-$$" port paths
    say "Community image: $image"

    for module in $ENTERPRISE_MODULES; do
        if in_image "$image" "import $module"; then
            fail "\`import $module\` works in the Community image"
        else
            ok "\`import $module\` fails"
        fi
    done
    if docker run --rm "$image" sh -c 'test ! -e /app/enterprise' ; then ok "no /app/enterprise"; else fail "/app/enterprise is in the image"; fi
    if docker run --rm "$image" sh -c 'test -f /app/app/app.py && ! find /app/app -name "*.pyc" | grep -q .'; then
        ok "the sources are in the image, no bytecode-only tree"
    else
        fail "the image does not hold the sources as sources"
    fi
    if docker run --rm "$image" sh -c 'test ! -e /app/.env'; then ok "no .env in the image"; else fail "a .env file is in the image"; fi
    if docker run --rm "$image" sh -c 'command -v aws gcloud az kubelogin' >/dev/null 2>&1; then
        fail "a cloud CLI is installed"
    else
        ok "no cloud CLI"
    fi
    if docker run --rm -e DATABASE_URL="$NO_DATABASE" "$image" brutor-agent-control-plane --help >/dev/null 2>&1; then
        ok "console script brutor-agent-control-plane runs"
    else
        fail "console script brutor-agent-control-plane does not run"
    fi

    database "$image" create "$db"
    DATABASES+=("$image|$db")

    # A first start without the administrator's password: refused.
    local refused="$name-refused" log
    docker run -d --name "$refused" "${HOST_ARGS[@]}" \
        -e DATABASE_URL="postgresql://$PG_USER:$PG_PASSWORD@$PG_HOST:$PG_PORT/$db" -e REDIS_URL="$REDIS_URL" \
        -e JWT_SECRET="$JWT_SECRET" -e ENCRYPTION_KEY="$ENCRYPTION_KEY" -e GUNICORN_WORKERS=1 "$image" >/dev/null
    CONTAINERS+=("$refused")
    local waited=0
    while [ "$(docker inspect -f '{{.State.Running}}' "$refused" 2>/dev/null)" = "true" ] && [ "$waited" -lt 150 ]; do
        sleep 2; waited=$((waited + 2))
    done
    if [ "$(docker inspect -f '{{.State.Running}}' "$refused" 2>/dev/null)" = "true" ]; then
        fail "without BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD the image keeps running"
    else
        log="$(docker logs "$refused" 2>&1 || true)"
        if printf '%s' "$log" | grep -q "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD is not set"; then
            ok "a first start without BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD is refused and names the variable"
        else
            fail "the refused start does not name BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD"
        fi
    fi
    docker rm -f "$refused" >/dev/null 2>&1 || true

    # The real start: no licence key, no JWT_ALGORITHM, no Qdrant, no MinIO.
    CONTAINERS+=("$name")
    port="$(start "$image" "$name" "$db" -e BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD="$TENANT_ADMIN_PASSWORD")"
    if wait_for_health "$name" "$port"; then
        ok "starts with no licence key and answers /health"
    else
        fail "does not answer /health within ${START_TIMEOUT}s"
        docker logs --tail 40 "$name" 2>&1 | sed 's/^/        /'
        return
    fi
    local tables; tables="$(tables_in "$image" "$db")"
    if [ "$tables" -gt 100 ]; then ok "created its schema ($tables tables)"; else fail "schema has $tables tables"; fi

    if sign_in "$port" /v1/admin/users/tenant/login \
        "$(python3 -c 'import json,sys; print(json.dumps({"username":"admin","password":sys.argv[1],"tenant_id":"default"}))' "$TENANT_ADMIN_PASSWORD")"; then
        ok "the tenant administrator signs in with the configured password"
    else
        fail "the tenant administrator cannot sign in with the configured password"
    fi
    for former in 'Admin123!'; do
        if sign_in "$port" /v1/admin/users/tenant/login \
            "$(python3 -c 'import json,sys; print(json.dumps({"username":"admin","password":sys.argv[1],"tenant_id":"default"}))' "$former")"; then
            fail "the tenant administrator signs in with the former default password"
        else
            ok "the former default password is refused"
        fi
    done
    if sign_in "$port" /v1/admin/users/tenant/login \
        "$(python3 -c 'import json,sys; print(json.dumps({"username":"admin-readonly","password":sys.argv[1],"tenant_id":"default"}))' "$TENANT_ADMIN_PASSWORD")"; then
        fail "a read-only administrator account exists"
    else
        ok "no read-only administrator account"
    fi

    paths="$(openapi_paths "$port")"
    if [ "$(printf '%s\n' "$paths" | wc -l)" -gt 300 ]; then ok "serves its OpenAPI document"; else fail "OpenAPI document is missing or small"; fi
    for path in $ENTERPRISE_PATHS; do
        if printf '%s\n' "$paths" | grep -qx "$path"; then fail "Enterprise route in the Community document: $path"; else ok "no $path"; fi
    done
}

# -----------------------------------------------------------------------------
smoke_enterprise() {
    local image="$1" db="control_smoke_enterprise_$$" name="control-smoke-enterprise-$$" port paths
    say "Enterprise image: $image"

    for module in $ENTERPRISE_MODULES; do
        if in_image "$image" "import sys; sys.path.insert(0, '/app/enterprise'); import $module"; then
            ok "\`import $module\` works"
        else
            fail "\`import $module\` fails in the Enterprise image"
        fi
    done
    if docker run --rm "$image" sh -c 'test ! -e /app/app/app.py && test -f /app/app/app.pyc && test -f /app/enterprise/brutor_enterprise/app.pyc && test ! -e /app/enterprise/brutor_enterprise/app.py'; then
        ok "bytecode only"
    else
        fail "the image holds sources, or is missing the bytecode"
    fi
    if docker run --rm "$image" sh -c 'test ! -e /app/.env && test ! -e /app/enterprise/tests'; then ok "no .env and no test tree in the image"; else fail ".env or enterprise/tests is in the image"; fi
    if docker run --rm -e DATABASE_URL="$NO_DATABASE" "$image" brutor-agent-control-plane-enterprise --help >/dev/null 2>&1; then
        ok "console script brutor-agent-control-plane-enterprise runs"
    else
        fail "console script brutor-agent-control-plane-enterprise does not run"
    fi

    # No valid licence, no Enterprise control plane:
    # the console script's `init` refuses before it connects to anything,
    # exits 1 and names the variable.
    if docker run --rm -e DATABASE_URL="$NO_DATABASE" "$image" brutor-agent-control-plane-enterprise init 2>&1 \
        | grep -q "BRUTOR_LICENSE_KEY environment variable is not set"; then
        ok "without BRUTOR_LICENSE_KEY the start is refused and names the variable"
    else
        fail "started, or failed for another reason, without BRUTOR_LICENSE_KEY"
    fi
    # The real start needs a licence Brutor AI signed: the smoke test is
    # given one in its environment (it is never printed) or stops here.
    if [ -z "${BRUTOR_LICENSE_KEY:-}" ]; then
        echo "    SKIP  the Enterprise start: set BRUTOR_LICENSE_KEY to a valid licence to run it"
        return
    fi

    database "$image" create "$db"
    DATABASES+=("$image|$db")
    CONTAINERS+=("$name")
    port="$(start "$image" "$name" "$db" -e BRUTOR_LICENSE_KEY \
        -e BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD="$TENANT_ADMIN_PASSWORD" -e BRUTOR_SYSTEM_ADMIN_PASSWORD="$BRUTOR_SYSTEM_ADMIN_PASSWORD")"
    if wait_for_health "$name" "$port"; then
        ok "starts and answers /health"
    else
        fail "does not answer /health within ${START_TIMEOUT}s"
        docker logs --tail 40 "$name" 2>&1 | sed 's/^/        /'
        return
    fi
    if sign_in "$port" /v1/admin/users/system/login \
        "$(python3 -c 'import json,sys; print(json.dumps({"username":"sysadmin","password":sys.argv[1]}))' "$BRUTOR_SYSTEM_ADMIN_PASSWORD")"; then
        ok "the system administrator signs in with the configured password"
    else
        fail "the system administrator cannot sign in with the configured password"
    fi
    paths="$(openapi_paths "$port")"
    for path in $ENTERPRISE_PATHS; do
        if printf '%s\n' "$paths" | grep -qx "$path"; then ok "has $path"; else fail "Enterprise route missing: $path"; fi
    done
}

[ -n "$COMMUNITY_IMAGE" ] && smoke_community "$COMMUNITY_IMAGE"
[ -n "$ENTERPRISE_IMAGE" ] && smoke_enterprise "$ENTERPRISE_IMAGE"

if [ "$FAILED" -ne 0 ]; then
    say "SMOKE FAILED"
    exit 1
fi
say "SMOKE PASSED"
