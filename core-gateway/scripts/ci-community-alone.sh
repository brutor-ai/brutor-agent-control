#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# The Community workspace, alone: every gate and the whole suite of the root
# workspace with the Enterprise workspace (`enterprise/`) ABSENT.
#
# What it proves: nothing the Community Edition is built or tested from
# reaches into `enterprise/`, a private dependency or a sibling repository.
# It is the whole of the CI job `community-alone` (.github/workflows/ci.yml),
# which removes `enterprise/` from its checkout and runs this; it uses no
# repository secret.
#
#   scripts/ci-community-alone.sh                      # every step
#   scripts/ci-community-alone.sh --prepare-template   # CI only: also stamp the
#                                                      # schema into template1 of
#                                                      # the (disposable) server
#
# It refuses to run where `enterprise/` exists: it never deletes anything.
# To run it by hand, copy the tracked files without `enterprise/` somewhere
# else and run it there:
#
#   git ls-files -z | grep -zv '^enterprise/' | rsync -a --from0 --files-from=- . /tmp/community/
#
# Needs: a throwaway Postgres and Redis (DATABASE_URL, TEST_REDIS_URL; see
# tests/integration/README.md), cargo-nextest, cargo-deny, cargo-about 0.9.3
# (scripts/third-party-licenses.sh), python3.
#
# Every step runs even when an earlier one failed, and the summary at the end
# names each. A sibling repository is looked for beside this one under its
# repository name and then under its directory in the public monorepo
# (../brutor-gateway-control, else ../control-plane; tests/support/roots.rs),
# so in the monorepo the cross-repository tests run. A test that needs a
# sibling which is not there (the control plane's interoperability fixtures,
# the edition boundary file) is skipped, and the summary lists every such
# test with where it was looked for: a skip is never silent.
# Exit status: 0 only when every step passed.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

PREPARE_TEMPLATE=0
for arg in "$@"; do
  case "$arg" in
    --prepare-template) PREPARE_TEMPLATE=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ -e enterprise ]; then
  echo "enterprise/ is here: this script proves the Community workspace stands alone," >&2
  echo "so it runs only where the Enterprise workspace is absent. It deletes nothing;" >&2
  echo "run it in a copy without enterprise/ (see the head of this file)." >&2
  exit 2
fi

# The Enterprise root is absent by construction: a variable left over from
# another run must not point the guards at one.
unset BRUTOR_ENTERPRISE_ROOT BRUTOR_COMMUNITY_ROOT

# A sibling repository that is not there skips the tests that need it, and
# each skip is recorded here (tests/support/roots.rs).
export BRUTOR_TEST_SIBLINGS=optional
SKIP_LOG="$(mktemp "${TMPDIR:-/tmp}/community-alone-skips.XXXXXX")"
export BRUTOR_TEST_SKIP_LOG="$SKIP_LOG"

NAMES=()
RESULTS=()
step() {
  local name="$1"
  shift
  echo
  echo "=== $name: $*"
  if "$@"; then
    RESULTS+=("PASS")
  else
    RESULTS+=("FAIL")
  fi
  NAMES+=("$name")
}

# No file of the Community tree is a link out of it, and the manifest names
# no path outside it: the build below would otherwise succeed here only
# because of where this copy happens to stand.
self_contained() {
  local bad=0 link target
  while IFS= read -r link; do
    target="$(cd "$(dirname "$link")" && realpath "$(readlink "$(basename "$link")")" 2>/dev/null || true)"
    case "$target" in
      "$PWD"/*) ;;
      *) echo "a link out of the tree: $link -> $(readlink "$link")"; bad=1 ;;
    esac
  done < <(find . -type l -not -path './target/*' -not -path './.git/*')
  # A member under crates/ may name another member (`../brutor-cose`);
  # nothing may name a path above the workspace root.
  if grep -nE 'path *= *"(\.\./|/)' Cargo.toml \
     || grep -nE 'path *= *"(\.\./\.\./|/)' crates/*/Cargo.toml; then
    echo "a manifest names a path outside the workspace"
    bad=1
  fi
  if grep -nE '^source = "git\+' Cargo.lock; then
    echo "Cargo.lock holds a git dependency"
    bad=1
  fi
  return $bad
}

step "self-contained"  self_contained
step "fmt"             cargo fmt --all -- --check
step "clippy"          cargo clippy --locked --workspace --all-targets --all-features -- -D warnings
step "deny"            cargo deny --locked check
step "third-party-licenses" scripts/third-party-licenses.sh --check
step "lint-ratchet"    scripts/lint-ratchet.sh
step "schema-superset" ./tests/schema/gen-test-superset.py --check
if [ "$PREPARE_TEMPLATE" = 1 ]; then
  step "prepare-template" env BRUTOR_TEST_DB_IS_DISPOSABLE=1 tests/schema/prepare-test-template.sh
fi
step "tests"           cargo nextest run --locked --workspace --lib --tests --features db-tests ${NEXTEST_PROFILE:+--profile "$NEXTEST_PROFILE"}
step "benches-compile" cargo bench --no-run --locked
# The run must not have conjured the directory it claims to do without.
step "still-alone"     test ! -e enterprise

echo
echo "=== Community-alone summary"
status=0
for i in "${!NAMES[@]}"; do
  printf '  %-22s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}"
  [ "${RESULTS[$i]}" = PASS ] || status=1
done
if [ -s "$SKIP_LOG" ]; then
  skipped="$(sort -u "$SKIP_LOG")"
  echo
  echo "  SKIPPED: $(printf '%s\n' "$skipped" | wc -l | tr -d ' ') test(s) need a sibling repository that is not here:"
  printf '%s\n' "$skipped" | awk -F'\t' '{printf "    %s  (needs %s, looked for at %s)\n", $1, $2, $3}'
else
  echo
  echo "  SKIPPED: none"
fi
rm -f "$SKIP_LOG"
echo
if [ "$status" = 0 ]; then
  echo "Community-alone: PASS"
else
  echo "Community-alone: FAIL"
fi
exit $status
