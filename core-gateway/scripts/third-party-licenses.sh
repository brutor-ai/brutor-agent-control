#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# The third-party licences file: every crate the binary is built from, its
# version, its SPDX licence and the full licence texts (one copy of each
# distinct text), from the committed Cargo.lock, by cargo-about.
#
# Third-party packages ship unmodified with their licence texts, in each
# image and in the repository.
#
#   scripts/third-party-licenses.sh                       # rewrite THIRD_PARTY_LICENSES
#   scripts/third-party-licenses.sh --check               # fail when it is not current
#   scripts/third-party-licenses.sh --check --out FILE    # the image build: also write
#                                                         # the generated file to FILE
#   scripts/third-party-licenses.sh --workspace enterprise [...]   # the Enterprise
#                                                         # workspace's file
#
# Configuration: <workspace>/about.toml (the accepted licences are deny.toml's
# allow-list; a crate under any other licence, or one whose licence cannot be
# read, fails here as it fails `cargo deny`) and <workspace>/about.hbs (the
# layout). Offline (`--frozen`): the licence texts are the ones in the
# downloaded crates, so the file is the same on every machine; a crate that
# ships no licence file gets the licence's standard text.
#
# Needs cargo-about 0.9.3:
#   cargo install --locked --features cli cargo-about@0.9.3
# (or CARGO_ABOUT=/path/to/cargo-about).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=0.9.3
CARGO_ABOUT="${CARGO_ABOUT:-cargo-about}"
ws=.
check=0
out=
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=1 ;;
    --out) out="${2:?--out needs a file}"; shift ;;
    --workspace) ws="${2:?--workspace needs a directory}"; shift ;;
    *) echo "usage: $0 [--workspace DIR] [--check] [--out FILE]" >&2; exit 2 ;;
  esac
  shift
done

have="$("$CARGO_ABOUT" --version 2>/dev/null || true)"
if [ "$have" != "cargo-about $VERSION" ]; then
  echo "needs cargo-about $VERSION (cargo install --locked --features cli cargo-about@$VERSION); found: ${have:-none}" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The crates must be on disk for the offline run (a no-op when they are).
cargo fetch --locked --manifest-path "$ws/Cargo.toml" >/dev/null
"$CARGO_ABOUT" generate --frozen --fail --threshold 0.93 \
  --manifest-path "$ws/Cargo.toml" -c "$ws/about.toml" \
  -o "$tmp/raw" "$ws/about.hbs"

# LF line ends (some crates' licence files have CRLF), and the count of the
# dependency lines into the `Dependencies (N)` header.
tr -d '\r' <"$tmp/raw" >"$tmp/lf"
awk 'NR == FNR { if (/^Dependencies \(/) { d = 1; next } if (d && /^$/) d = 0; if (d) n++; next }
     { sub(/@@COUNT@@/, n) } 1' "$tmp/lf" "$tmp/lf" >"$tmp/THIRD_PARTY_LICENSES"

committed="$ws/THIRD_PARTY_LICENSES"
hint=
[ "$ws" = . ] || hint=" --workspace $ws"
[ -z "$out" ] || cp "$tmp/THIRD_PARTY_LICENSES" "$out"
if [ "$check" = 1 ]; then
  if ! diff -u "$committed" "$tmp/THIRD_PARTY_LICENSES" >"$tmp/diff"; then
    head -n 40 "$tmp/diff" >&2
    echo "$committed is not current against $ws/Cargo.lock: run scripts/third-party-licenses.sh$hint and commit it" >&2
    exit 1
  fi
  echo "$committed is current"
else
  cp "$tmp/THIRD_PARTY_LICENSES" "$committed"
  echo "wrote $committed ($(awk '/^Dependencies \(/ { print $2 }' "$committed" | tr -d '()') crates)"
fi
