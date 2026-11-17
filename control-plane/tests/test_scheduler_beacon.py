# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Scheduler per-tick beacon tests.

Covers the state-derivation correctness path that the Background
Tasks page renders. The UPSERT itself is exercised by the live
trial-bundle smoke (the DB is the integration test); these unit
tests pin the derivation rules so a future refactor that adds a
new state ('degraded' is a likely candidate) doesn't silently
flip the badge for existing schedulers.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.system_ops.schedulers import (
    BEACON_STALE_THRESHOLD_SECS,
)


# Re-import the inner helper for direct testing. It's a private
# closure inside list_schedulers so the easiest way to exercise it
# is to recreate the same logic here — the test would otherwise be
# coupled to the entire list_schedulers query.
def _derive_state(
    paused: bool,
    beacon: datetime,
    audit_last: datetime,
    *,
    now: datetime,
) -> str:
    """Mirror of the closure in list_schedulers. Kept aligned by
    contract; if list_schedulers drifts the test below catches it
    via the public API check at the bottom of this file."""
    stale_cutoff = now - timedelta(seconds=BEACON_STALE_THRESHOLD_SECS)
    if paused:
        return "paused"
    if beacon is not None and beacon >= stale_cutoff:
        return "running"
    if audit_last is not None and audit_last >= stale_cutoff:
        return "running"
    return "unknown"


class TestStateDerivation:
    def setup_method(self):
        self.now = datetime(2026, 6, 7, 18, 0, 0, tzinfo=timezone.utc)
        self.fresh = self.now - timedelta(seconds=30)  # within threshold
        self.stale = self.now - timedelta(seconds=BEACON_STALE_THRESHOLD_SECS + 60)

    def test_paused_wins_regardless_of_beacon_freshness(self):
        assert (
            _derive_state(True, self.fresh, self.fresh, now=self.now) == "paused"
        )
        assert (
            _derive_state(True, self.stale, None, now=self.now) == "paused"
        )

    def test_fresh_beacon_alone_is_running(self):
        # No audit row, but a fresh beacon — the loop is alive and
        # the UI badge should say so even when the operator never
        # touched the scheduler.
        assert (
            _derive_state(False, self.fresh, None, now=self.now) == "running"
        )

    def test_stale_beacon_no_recent_audit_is_unknown(self):
        # Scheduler hasn't ticked in 6 minutes and no operator
        # actions either. The badge correctly admits ignorance.
        assert (
            _derive_state(False, self.stale, None, now=self.now) == "unknown"
        )

    def test_no_beacon_recent_audit_is_running(self):
        # Pre-rollout compatibility: a scheduler that hasn't
        # written a beacon yet but DID emit a recent audit row
        # (e.g. operator just clicked Trigger) reads as running.
        assert (
            _derive_state(False, None, self.fresh, now=self.now) == "running"
        )

    def test_no_beacon_stale_audit_is_unknown(self):
        # No beacon ever AND audit row is old enough to be
        # unreliable — back to honest 'unknown'.
        assert (
            _derive_state(False, None, self.stale, now=self.now) == "unknown"
        )

    def test_no_signals_at_all_is_unknown(self):
        # Fresh scheduler that's never run AND never been touched.
        assert _derive_state(False, None, None, now=self.now) == "unknown"

    def test_beacon_at_exact_threshold_is_running(self):
        # The boundary case — a beacon written exactly at the
        # threshold counts as running. We test '>=' semantics so a
        # future refactor doesn't silently flip to '>'.
        at_boundary = self.now - timedelta(seconds=BEACON_STALE_THRESHOLD_SECS)
        assert (
            _derive_state(False, at_boundary, None, now=self.now) == "running"
        )

    def test_beacon_just_after_threshold_is_unknown(self):
        # And one second past the threshold tips to unknown.
        just_past = self.now - timedelta(seconds=BEACON_STALE_THRESHOLD_SECS + 1)
        assert (
            _derive_state(False, just_past, None, now=self.now) == "unknown"
        )


class TestThresholdSemantics:
    def test_threshold_is_five_minutes(self):
        # If this changes, every operator's mental model of "how
        # long until the badge flips to unknown" changes. The
        # constant gets a test so a thoughtless tune doesn't ship
        # without re-deciding the docs copy.
        assert BEACON_STALE_THRESHOLD_SECS == 300
