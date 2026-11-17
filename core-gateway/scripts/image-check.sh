#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# What a built gateway image says about itself, and what it holds.
#
#   scripts/image-check.sh community|enterprise <image>
#
# Starts nothing (it reads the image with `docker inspect` and `docker save`),
# so it needs no database; `scripts/image-smoke.sh` runs it before it starts
# the image.
#
# 1. Labels. The image carries the labels of its edition's line in
#    `scripts/image-names.txt`: `org.opencontainers.image.title`,
#    `.licenses` (`Apache-2.0` for the Community image, the proprietary
#    identifier for the Enterprise one), `.source`, a non-empty
#    `.description`, `.vendor` = "Brutor AI Ltd" and `ai.brutor.edition`. It
#    carries no `maintainer` label (a person's name is not image metadata) and
#    no `version` label outside the `org.opencontainers.image.` namespace.
#
# 2. Contents, Community image only. The image holds nothing of the
#    Enterprise Edition: no Enterprise binary, no file from `enterprise/`, no
#    knowledge-base connector icon as a file, and the gateway binary does not
#    carry the connector-icon route, the Enterprise crate or the licence crate.
#
# Exit code 0: the image is as stated. Anything else: it is not.
# =============================================================================
set -euo pipefail

edition="${1:-}"
image="${2:-}"
case "$edition" in
  community|enterprise) ;;
  *) echo "usage: $0 community|enterprise <image>" >&2; exit 2 ;;
esac
[ -n "$image" ] || { echo "usage: $0 community|enterprise <image>" >&2; exit 2; }

here="$(cd "$(dirname "$0")" && pwd)"
list="$here/image-names.txt"
line="$(grep -v '^#' "$list" | awk -F'\t' -v e="$edition" '$1==e')"
[ -n "$line" ] || { echo "FAIL: $list has no line for $edition" >&2; exit 1; }
want_title="$(printf '%s' "$line" | cut -f3)"
want_licenses="$(printf '%s' "$line" | cut -f4)"
want_source="$(printf '%s' "$line" | cut -f5)"

fail=0
label() {
  docker inspect -f "{{ index .Config.Labels \"$1\" }}" "$image" 2>/dev/null || true
}
expect() {
  local name="$1" want="$2" have
  have="$(label "$name")"
  if [ "$have" = "$want" ]; then
    echo "OK: $name = $want"
  else
    echo "FAIL: $name is '${have:-<none>}', not '$want'" >&2
    fail=1
  fi
}

docker inspect "$image" >/dev/null 2>&1 || { echo "FAIL: no image $image" >&2; exit 1; }

expect "ai.brutor.edition" "$edition"
expect "org.opencontainers.image.title" "$want_title"
expect "org.opencontainers.image.licenses" "$want_licenses"
expect "org.opencontainers.image.source" "$want_source"
expect "org.opencontainers.image.vendor" "Brutor AI Ltd"
description="$(label org.opencontainers.image.description)"
if [ -n "$description" ] && [ "$description" != "<no value>" ]; then
  echo "OK: org.opencontainers.image.description is set"
else
  echo "FAIL: org.opencontainers.image.description is not set" >&2
  fail=1
fi
for gone in maintainer version description; do
  have="$(label "$gone")"
  if [ -n "$have" ] && [ "$have" != "<no value>" ]; then
    echo "FAIL: the image carries the label $gone='$have' (use org.opencontainers.image.*)" >&2
    fail=1
  fi
done

if [ "$edition" = community ]; then
  work="$(mktemp -d "${TMPDIR:-/tmp}/image-check.XXXXXX")"
  trap 'rm -r "$work"' EXIT
  docker save "$image" -o "$work/image.tar"
  mkdir "$work/image" "$work/root"
  tar -xf "$work/image.tar" -C "$work/image"
  # Every layer, whatever the archive's layout: a layer is a tar (plain or
  # gzip) somewhere under the saved image.
  : > "$work/names"
  layers=0
  while IFS= read -r blob; do
    if tar -tf "$blob" > "$work/listing" 2>/dev/null; then
      layers=$((layers + 1))
      cat "$work/listing" >> "$work/names"
      if grep -qx 'app/brutor-gateway-core' "$work/listing"; then
        tar -xf "$blob" -C "$work/root" app/brutor-gateway-core
      fi
    fi
  done < <(find "$work/image" -type f)
  if [ "$layers" -lt 2 ] || ! grep -q '^usr/' "$work/names"; then
    echo "FAIL: the image's layers could not be read ($layers found)" >&2
    exit 1
  fi
  echo "OK: read $layers layers, $(wc -l < "$work/names" | tr -d ' ') entries"

  bad="$(grep -iE '(^|/)enterprise(/|$)|brutor-gateway-enterprise|connector[_-]icons|(^|/)(confluence|notion|google_drive|slack|github|web_crawler|jira|sharepoint)\.svg$' "$work/names" || true)"
  if [ -n "$bad" ]; then
    echo "FAIL: the Community image holds Enterprise files or connector icons:" >&2
    printf '%s\n' "$bad" | head -20 >&2
    fail=1
  else
    echo "OK: no file from enterprise/ and no connector icon in the image"
  fi

  binary="$work/root/app/brutor-gateway-core"
  if [ ! -s "$binary" ]; then
    echo "FAIL: the image has no /app/brutor-gateway-core" >&2
    fail=1
  else
    # The control: the scan can see a string the Community binary does carry.
    if ! grep -aq '/v1/assets' "$binary"; then
      echo "FAIL: the binary scan is blind (it does not find '/v1/assets' in the gateway binary)" >&2
      fail=1
    fi
    for needle in 'connector-icons' 'connector_icons' 'brutor_gateway_enterprise' 'brutor_license'; do
      if grep -aq -- "$needle" "$binary"; then
        echo "FAIL: the Community binary carries '$needle'" >&2
        fail=1
      fi
    done
    [ "$fail" = 0 ] && echo "OK: the Community binary carries no connector-icon route and no Enterprise or licence crate"
  fi
fi

if [ "$fail" = 0 ]; then
  echo "OK: $image is the $edition image it says it is"
fi
exit $fail
