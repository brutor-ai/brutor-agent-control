#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# Brutor Agent Control Community Edition - start.
#
# Optional: `docker compose up -d` is all a downloaded package needs. This
# script adds three things around it: it checks that Docker is there, it fills
# in any generated value that is still empty in .env (so a clone of the source
# repository works too), and it waits until the stack is healthy.
# It asks no questions.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=.env

fail() { echo "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. See https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (the 'docker compose' command)."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start it and run this again."
[ -f "$ENV_FILE" ] || fail "$ENV_FILE is missing. Run this from the package folder."

rand_hex() {  # rand_hex <bytes>
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    LC_ALL=C od -An -N "$1" -tx1 /dev/urandom | tr -d ' \n'
  fi
}

# Fill in KEY=<value> when the line is `KEY=` with nothing after it. A value
# that is already there is never touched.
mint() {  # mint <KEY> <value>
  local key=$1 value=$2 tmp
  grep -Eq "^${key}=[[:space:]]*$" "$ENV_FILE" || return 0
  tmp=$(mktemp)
  awk -v k="$key" -v v="$value" '
    $0 ~ "^" k "=[ \t]*$" && !done { print k "=" v; done = 1; next }
    { print }
  ' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
  echo "  generated $key"
}

echo "Checking $ENV_FILE"
mint DB_PASSWORD                    "$(rand_hex 24)"
mint JWT_SECRET                     "$(rand_hex 32)"
mint ENCRYPTION_KEY                 "$(rand_hex 32)"
mint BRUTOR_GATEWAY_SERVICE_API_KEY "sk_brutor_api_$(rand_hex 24)"
mint BRUTOR_SKILL_RUNNER_JWT_SECRET "$(rand_hex 32)"
mint BRUTOR_EVIDENCE_SIGNING_KEY    "$(rand_hex 32)"
mint BRUTOR_AUDIT_SIGNING_KEY       "$(rand_hex 32)"
mint BRUTOR_WITNESS_SIGNING_KEY     "$(rand_hex 32)"
mint BRUTOR_DEPLOYMENT_ID           "brutor-$(rand_hex 8)"
mint BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD  "$(rand_hex 12)"

value_of() { sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1; }

# A value that is there is never replaced, so one that a component would
# refuse has to stop the start here, with its name: the compose file only
# notices an empty line, and the component's own message comes later and from
# inside a container. The value itself is not printed.
BAD_VALUES=0
check() {  # check <KEY> <pattern for the whole value> <the rule in words>
  if ! value_of "$1" | grep -Eq "^($2)\$"; then
    echo "ERROR: $1 in $ENV_FILE is not usable: it must be $3." >&2
    BAD_VALUES=$((BAD_VALUES + 1))
  fi
}
no_placeholder() {  # the core and the control plane refuse these, in any case
  if value_of "$1" | grep -Eiq 'your-|change-this|changeme|example'; then
    echo "ERROR: $1 in $ENV_FILE is not usable: it contains your-, change-this, changeme or example," >&2
    echo "       which the services take for the value of an example file." >&2
    BAD_VALUES=$((BAD_VALUES + 1))
  fi
}
check DB_PASSWORD                    '[A-Za-z0-9._~-]{8,}' "at least 8 letters, digits, ., _, ~ or - (it is written into a database URL)"
check JWT_SECRET                     '.{32,}' "at least 32 characters"
check ENCRYPTION_KEY                 '.{32,}' "at least 32 characters"
check BRUTOR_GATEWAY_SERVICE_API_KEY 'sk_brutor_api_[A-Za-z0-9_-]{32,}' "sk_brutor_api_ followed by at least 32 letters, digits, - or _"
check BRUTOR_SKILL_RUNNER_JWT_SECRET '.{32,}' "at least 32 characters"
check BRUTOR_EVIDENCE_SIGNING_KEY    '[0-9a-fA-F]{64}' "exactly 64 hex characters"
check BRUTOR_AUDIT_SIGNING_KEY       '[0-9a-fA-F]{64}' "exactly 64 hex characters"
check BRUTOR_WITNESS_SIGNING_KEY     '[0-9a-fA-F]{64}' "exactly 64 hex characters"
check BRUTOR_DEPLOYMENT_ID           '.+' "not empty"
check BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD  '.{8,}' "at least 8 characters"
no_placeholder JWT_SECRET
no_placeholder ENCRYPTION_KEY
if [ "$BAD_VALUES" -gt 0 ]; then
  cat >&2 <<'HELP'

Nothing was started and .env was not changed. For each line named above:
  - before the first start, empty it (NAME= with nothing after the equals
    sign) and run this script again to have a value generated, or set a
    value that fits the rule;
  - on an installation that has already run, put back the value it was
    started with: a new secret does not open data stored under the old one.
HELP
  exit 1
fi
# What Compose substitutes for ${NAME:-default} in docker-compose.yml: the
# shell's variable when it is set, else the line in .env, else the compose
# file's default. The addresses printed below are the ones Compose publishes.
setting_of() {  # setting_of <NAME> <the compose file's default>
  local name=$1 fallback=$2 value
  value=${!name:-}
  if [ -z "$value" ]; then
    value=$(value_of "$name" | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
  fi
  echo "${value:-$fallback}"
}
ADMIN_CONSOLE_PORT=$(setting_of BRUTOR_ADMIN_CONSOLE_PORT 3002)
CONTROL_PLANE_PORT=$(setting_of BRUTOR_CONTROL_PLANE_PORT 5050)
GATEWAY_PORT=$(setting_of BRUTOR_GATEWAY_PORT 8100)
ADMIN_USER=$(setting_of BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME admin)

echo "Starting (the first start pulls the images and sets up the database; allow a few minutes)"
docker compose up -d --wait

cat <<DONE

Brutor Agent Control Community Edition is running.

  Admin Console   http://localhost:${ADMIN_CONSOLE_PORT}
  Control plane   http://localhost:${CONTROL_PLANE_PORT}
  Core gateway    http://localhost:${GATEWAY_PORT}

Sign in to the Admin Console as "${ADMIN_USER}". The password is the value of
BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD in the .env file in this folder.

Stop:    docker compose down
Remove:  docker compose down -v   (deletes all data)
DONE
