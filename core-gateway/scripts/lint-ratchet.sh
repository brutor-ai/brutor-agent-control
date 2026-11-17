#!/usr/bin/env bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Lint-suppression ratchet.
#
# Counts `#[allow(...)]` / `#![allow(...)]` / `#[cfg_attr(..., allow(...))]`
# per lint name across src/, crates/, tests/ and the Enterprise workspace
# (its src/, tests/ and benches/), and compares with the
# committed baseline. FAILS when any lint's count rises or a new lint appears;
# reports when a count falls so the baseline can be lowered. Counts only ever
# go down — the same model as the UIs' `--max-warnings` caps.
#
#   scripts/lint-ratchet.sh           check (CI, prepush-check.sh)
#   scripts/lint-ratchet.sh --update  rewrite the baseline — ONLY after
#                                     removing suppressions, never to add one
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
BASELINE=scripts/lint-ratchet.baseline

# The roots counted: the Community crate and its evidence crates, and the
# Enterprise workspace where this repository carries it
# (ONE count over both, so moving code between them cannot
# hide a suppression and the total cannot rise). A lint allowed in a
# manifest's `[lints]` table would be a suppression too: there is none (the
# evidence crates' tables only forbid), and the check below fails if one
# appears, until this script counts such entries.
#
# Where the Enterprise workspace is: BRUTOR_ENTERPRISE_ROOT when it is given
# (the same variable the tests read, tests/support/roots.rs), else
# `enterprise/` here when it is there. A root that was GIVEN and holds no
# source fails the check: counting nothing is not a pass. Not given and not
# there is a Community tree alone, and only the Community roots are counted
# (against the same baseline, so there the check can only report).
ROOTS=(src crates tests)
ENTERPRISE_ROOT="${BRUTOR_ENTERPRISE_ROOT:-enterprise}"
if [ -n "${BRUTOR_ENTERPRISE_ROOT:-}" ] && ! find "$ENTERPRISE_ROOT/src" -name '*.rs' 2>/dev/null | grep -q .; then
  echo "BRUTOR_ENTERPRISE_ROOT=$ENTERPRISE_ROOT holds no Rust source under src/: the Enterprise workspace is not there"
  exit 1
fi
ALONE=1
for extra in src tests benches; do
  [ -d "$ENTERPRISE_ROOT/$extra" ] && ROOTS+=("$ENTERPRISE_ROOT/$extra") && ALONE=0
done
for manifest in Cargo.toml "$ENTERPRISE_ROOT/Cargo.toml" crates/*/Cargo.toml; do
  if [ -f "$manifest" ] && grep -qE '^[A-Za-z_:-]+ *= *(\{.*level *= *)?"allow"' "$manifest"; then
    echo "$manifest allows a lint in a [lints] table: a suppression this ratchet does not count yet"
    exit 1
  fi
done

current() {
  grep -rhoE --include='*.rs' '#!?\[(cfg_attr\([^]]*, *)?allow\([^]]*\)' "${ROOTS[@]}" \
    | sed -E 's/.*allow\(//; s/\).*//' \
    | tr ',' '\n' | sed -E 's/^ +| +$//g' | grep -v '^$' \
    | sort | uniq -c | awk '{print $2"\t"$1}' | sort
}

if [ "${1:-}" = "--update" ]; then
  if [ "$ALONE" = 1 ]; then
    echo "the Enterprise workspace is not here: the baseline is the count over both, and is not rewritten from a Community tree alone"
    exit 1
  fi
  current > "$BASELINE"
  echo "baseline rewritten: $(awk -F'\t' '{s+=$2} END {print s}' "$BASELINE") suppressions"
  exit 0
fi

[ -f "$BASELINE" ] || { echo "missing $BASELINE (run with --update once)"; exit 1; }

fail=0
while IFS=$'\t' read -r lint n; do
  base=$(awk -F'\t' -v l="$lint" '$1==l {print $2}' "$BASELINE")
  if [ -z "$base" ]; then
    echo "NEW suppression: $lint ($n) — remove it, or justify it in review and lower another"
    fail=1
  elif [ "$n" -gt "$base" ]; then
    echo "MORE suppressions of $lint: $base -> $n"
    fail=1
  elif [ "$n" -lt "$base" ] && [ "$ALONE" = 0 ]; then
    echo "fewer suppressions of $lint: $base -> $n (lower the baseline: --update)"
  fi
done < <(current)

total=$(current | awk -F'\t' '{s+=$2} END {print s+0}')
[ "$ALONE" = 1 ] && echo "a Community tree alone: the Enterprise workspace's suppressions are not counted here, so a count below the baseline is expected"
echo "lint suppressions: $total (baseline $(awk -F'\t' '{s+=$2} END {print s+0}' "$BASELINE"))"
exit $fail
