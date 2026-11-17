#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Start smoke of a built gateway image.
#
# The image workflow used to "test" the image with
# `docker run … <binary> --help || true`. The gateway has no `--help` (it
# panics with "DATABASE_URL must be set"), and `|| true` accepts any outcome,
# so the step proved nothing. This script asserts what each edition's image
# must do when it is started:
#
# Both editions: the image first passes `scripts/image-check.sh` (its labels
# are the ones of `scripts/image-names.txt`; the Community image holds no
# Enterprise file and no connector icon).
#
#   scripts/image-smoke.sh community <image>
#       The image is labelled `ai.brutor.edition=community`. The Community
#       gateway starts with no licence key, on a Postgres that holds the
#       schema and a reachable Redis, `/health` answers 200 and
#       `/v1/version` answers `"edition": "community"`. It runs as one
#       instance: a second container of the same image on the
#       same Redis and database does not start. It exits non-zero and its
#       output names the Enterprise Edition. The first keeps answering.
#       The Redis must hold no live instance lease of another gateway (key
#       `brutor:instance:core` in the database SMOKE_REDIS_URL names): the
#       smoke's own first container would be the one refused.
#       Needs (as the CONTAINER sees them):
#         SMOKE_DATABASE_URL   a throwaway database with the schema loaded
#         SMOKE_REDIS_URL      a reachable Redis
#       Optional:
#         SMOKE_DOCKER_ARGS    extra `docker run` arguments (e.g. `--network x`)
#
#   scripts/image-smoke.sh enterprise <image>
#       The image is labelled `ai.brutor.edition=enterprise`. The Enterprise
#       gateway refuses to start without a licence key: the process exits
#       with code 1, before anything is connected (so it needs no database),
#       and says why. (Its `/v1/version` answer is asserted by the Enterprise
#       suite: an image without a key serves nothing to ask.)
#
# Exit code 0: the image behaves as stated. Anything else: it does not.
# Never point SMOKE_DATABASE_URL at a database that is in use.
# =============================================================================
set -euo pipefail

edition="${1:-}"
image="${2:-}"
if [ -z "$edition" ] || [ -z "$image" ]; then
  echo "usage: $0 community|enterprise <image>" >&2
  exit 2
fi

# Values for the smoke only: 64 hex characters each, nothing a deployment uses.
SMOKE_JWT_SECRET="5m0ke-5m0ke-5m0ke-5m0ke-5m0ke-5m0ke-5m0ke-5m0ke-5m0ke-5m0ke-5m0k"
SMOKE_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

name="image-smoke-$$"
second="image-smoke-$$-second"
cleanup() { docker rm -f "$name" "$second" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# What the image says about itself and what it holds, before it is started
# (`scripts/image-check.sh`): the labels of its edition's line in
# `scripts/image-names.txt` (release tooling tells the two images apart by
# `ai.brutor.edition`, so an image without the label, or with the other
# edition's, fails here), and for the Community image that it holds nothing
# of the Enterprise Edition.
case "$edition" in
  community|enterprise)
    "$(dirname "$0")/image-check.sh" "$edition" "$image"
    ;;
esac

case "$edition" in
  community)
    : "${SMOKE_DATABASE_URL:?set SMOKE_DATABASE_URL (a throwaway database with the schema, as the container sees it)}"
    : "${SMOKE_REDIS_URL:?set SMOKE_REDIS_URL (as the container sees it)}"
    case "$SMOKE_DATABASE_URL" in
      */ai_gateway|*/ai_gateway\?*|*/brutor|*/brutor\?*)
        echo "refusing to smoke against a database named ai_gateway or brutor" >&2
        exit 2
        ;;
    esac
    # shellcheck disable=SC2086  # SMOKE_DOCKER_ARGS is a list of arguments
    docker run -d --name "$name" ${SMOKE_DOCKER_ARGS:-} \
      -e DATABASE_URL="$SMOKE_DATABASE_URL" \
      -e REDIS_URL="$SMOKE_REDIS_URL" \
      -e JWT_SECRET="$SMOKE_JWT_SECRET" \
      -e ENCRYPTION_KEY="$SMOKE_ENCRYPTION_KEY" \
      "$image" >/dev/null

    # Up to 60 s for the listener; the container must still be running.
    for _ in $(seq 1 60); do
      if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != "true" ]; then
        echo "FAIL: the Community gateway exited (code $(docker inspect -f '{{.State.ExitCode}}' "$name")):" >&2
        docker logs "$name" 2>&1 | tail -40 >&2
        exit 1
      fi
      code="$(docker exec "$name" curl -s -o /dev/null -w '%{http_code}' http://localhost:8100/health 2>/dev/null || true)"
      if [ "$code" = "200" ]; then
        body="$(docker exec "$name" curl -s http://localhost:8100/health)"
        echo "OK: the Community gateway started with no licence key; /health answered 200: $body"
        # The binary states its edition itself, from what it is composed of.
        version="$(docker exec "$name" curl -s http://localhost:8100/v1/version)"
        if ! echo "$version" | grep -Eq '"edition"[[:space:]]*:[[:space:]]*"community"'; then
          echo "FAIL: /v1/version does not answer edition \"community\": $version" >&2
          exit 1
        fi
        echo "OK: /v1/version answers edition \"community\""

        # One instance. A second container of the same image
        # on the same Redis and database finds the first one's lease, waits
        # for it to lapse (20 s; it does not, the first renews it), and
        # exits non-zero naming the Enterprise Edition.
        # shellcheck disable=SC2086
        docker run -d --name "$second" ${SMOKE_DOCKER_ARGS:-} \
          -e DATABASE_URL="$SMOKE_DATABASE_URL" \
          -e REDIS_URL="$SMOKE_REDIS_URL" \
          -e JWT_SECRET="$SMOKE_JWT_SECRET" \
          -e ENCRYPTION_KEY="$SMOKE_ENCRYPTION_KEY" \
          "$image" >/dev/null
        state=""
        for _ in $(seq 1 60); do
          state="$(docker inspect -f '{{.State.Status}}' "$second" 2>/dev/null || true)"
          [ "$state" = "exited" ] && break
          sleep 1
        done
        second_code="$(docker inspect -f '{{.State.ExitCode}}' "$second" 2>/dev/null || true)"
        second_logs="$(docker logs "$second" 2>&1 | tail -20)"
        if [ "$state" != "exited" ]; then
          echo "FAIL: a second Community instance on the same Redis is still running after 60 s (state: ${state:-unknown})" >&2
          echo "$second_logs" >&2
          exit 1
        fi
        if [ "$second_code" = "0" ]; then
          echo "FAIL: the second Community instance exited with code 0" >&2
          echo "$second_logs" >&2
          exit 1
        fi
        if ! echo "$second_logs" | grep -q "Another Core Gateway instance is already running" \
          || ! echo "$second_logs" | grep -q "Enterprise Edition"; then
          echo "FAIL: the second instance exited with code $second_code, but its output does not carry the single-instance refusal that names the Enterprise Edition:" >&2
          echo "$second_logs" >&2
          exit 1
        fi
        echo "OK: a second Community instance on the same Redis was refused (exit code $second_code) and told about the Enterprise Edition"
        # The refusal did not disturb the first instance.
        code="$(docker exec "$name" curl -s -o /dev/null -w '%{http_code}' http://localhost:8100/health 2>/dev/null || true)"
        if [ "$code" != "200" ]; then
          echo "FAIL: the first instance stopped answering after the second was refused (/health: ${code:-nothing})" >&2
          docker logs "$name" 2>&1 | tail -40 >&2
          exit 1
        fi
        echo "OK: the first instance still answers /health 200"
        exit 0
      fi
      sleep 1
    done
    echo "FAIL: /health did not answer 200 within 60 s (last: ${code:-nothing}):" >&2
    docker logs "$name" 2>&1 | tail -40 >&2
    exit 1
    ;;

  enterprise)
    # No BRUTOR_LICENSE_KEY. The database URL names nothing reachable: the
    # licence gate must refuse before anything is connected.
    set +e
    docker run --name "$name" \
      -e DATABASE_URL="postgres://nobody:nothing@127.0.0.1:1/nothing" \
      -e REDIS_URL="redis://127.0.0.1:1/0" \
      -e JWT_SECRET="$SMOKE_JWT_SECRET" \
      -e ENCRYPTION_KEY="$SMOKE_ENCRYPTION_KEY" \
      "$image" >/dev/null 2>&1 &
    runner=$!
    # The refusal is immediate; a gateway still running after 30 s started.
    for _ in $(seq 1 30); do
      state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)"
      [ "$state" = "exited" ] && break
      sleep 1
    done
    state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)"
    code="$(docker inspect -f '{{.State.ExitCode}}' "$name" 2>/dev/null)"
    logs="$(docker logs "$name" 2>&1 | tail -20)"
    wait "$runner" 2>/dev/null
    set -e
    if [ "$state" != "exited" ]; then
      echo "FAIL: the Enterprise gateway is still running without a licence key (state: $state)" >&2
      echo "$logs" >&2
      exit 1
    fi
    if [ "$code" != "1" ]; then
      echo "FAIL: without a licence key the Enterprise gateway exited with code $code, not 1" >&2
      echo "$logs" >&2
      exit 1
    fi
    if ! echo "$logs" | grep -qi "licen[cs]e"; then
      echo "FAIL: exit code 1, but the output does not name the licence:" >&2
      echo "$logs" >&2
      exit 1
    fi
    echo "OK: the Enterprise gateway refused to start without a licence key (exit code 1)"
    exit 0
    ;;

  *)
    echo "usage: $0 community|enterprise <image>" >&2
    exit 2
    ;;
esac
