# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression suite for the Oversight signal (a154_oversight_signal).

The behaviours pinned here are the ones that make the signal *honest*
rather than merely present. Each has a failure mode that would be
invisible in a demo and embarrassing in an audit:

  * un-gated systems must score `None`, never 0 — otherwise every
    deliberately-autonomous system reads as badly-overseen;
  * `None` must degrade nothing in the worst-of roll-up;
  * an expired approval must cost score, because it is the one oversight
    failure with no benign reading;
  * a pending approval must NOT count as a lapse — it is in flight;
  * latency and denial rate must stay UNSCORED, because thresholding them
    manufactures findings (a fast approval may be a responsive team).

The scorer half is a pure function, so most of this runs without a DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database.models.ai_system_health import COMPONENTS, score_health
from app.services import ai_system_oversight as oversight
from tests.source_trees import python_sources, source_label


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COUNTS = ("requested", "approved", "rejected", "expired", "pending", "decided")


def facts(**over) -> dict:
    """An oversight-facts dict with sane zeros, overridden per test. No hold
    lies inside an incident window here, so the judged counts are the
    record's (exclusion: tests/test_judgement_exclusion_single_predicate.py)."""
    base = {
        "requested": 0,
        "approved": 0,
        "rejected": 0,
        "expired": 0,
        "pending": 0,
        "decided": 0,
        "lapse_rate": None,
        "denial_rate": None,
        "latency_p50_seconds": None,
        "distinct_approvers": 0,
        "excluded_in_incident_windows": 0,
    }
    base.update(over)
    base.setdefault("judged", {k: base[k] for k in _COUNTS})
    return base


# ---------------------------------------------------------------------------
# 1. Not-applicable is not a failure
# ---------------------------------------------------------------------------

class TestNotApplicable:
    def test_no_approvals_scores_none_not_zero(self):
        """The single most important rule.

        A system with no approval gate has no oversight *requirement*.
        Scoring it 0 would punish a deliberate design choice and drag the
        worst-of verdict of every autonomous system on the platform.
        """
        assert oversight.score_oversight(facts(requested=0)) is None

    def test_no_approvals_yields_no_reasons(self):
        assert oversight.oversight_reasons(facts(requested=0)) == []

    def test_report_section_marks_inapplicable_and_explains_why(self):
        section = oversight.oversight_report_section(facts(requested=0))
        assert section["applicable"] is False
        # The wording must point at the contract, not imply a fault.
        assert "contract question" in section["means"]

    def test_none_oversight_degrades_nothing_in_worst_of(self):
        """`None` must behave like every other insufficient-evidence signal."""
        with_signal = score_health(
            liveness=1.0, behaviour=1.0, reliability=1.0,
            cost=1.0, conformance=1.0, oversight=None,
        )
        assert with_signal["health_score"] == 1.0
        assert with_signal["oversight_score"] is None


# ---------------------------------------------------------------------------
# 2. Lapse rate — the scored spine
# ---------------------------------------------------------------------------

class TestLapseRate:
    def test_all_answered_scores_full(self):
        f = facts(requested=10, approved=8, rejected=2, decided=10,
                  expired=0, lapse_rate=0.0, distinct_approvers=3)
        assert oversight.score_oversight(f) == 1.0

    def test_expired_approvals_cost_score_proportionally(self):
        """3 of 10 lapsed → 0.7. A human was asked and never answered."""
        f = facts(requested=10, approved=7, decided=7, expired=3,
                  lapse_rate=0.3, distinct_approvers=3)
        assert oversight.score_oversight(f) == pytest.approx(0.7)

    def test_total_lapse_scores_zero(self):
        f = facts(requested=5, expired=5, lapse_rate=1.0)
        assert oversight.score_oversight(f) == 0.0

    def test_expiry_is_explained_to_the_operator(self):
        f = facts(requested=10, approved=7, decided=7, expired=3,
                  lapse_rate=0.3, distinct_approvers=3)
        reasons = oversight.oversight_reasons(f)
        assert any("expired without a human decision" in r for r in reasons)

    def test_pending_is_not_a_lapse(self):
        """A request still inside its window is in flight, not failed.

        Counting it would make every healthy system with an open approval
        look like an oversight failure for the duration of the window.
        """
        f = facts(requested=4, approved=3, decided=3, pending=1,
                  expired=0, lapse_rate=0.0, distinct_approvers=2)
        assert oversight.score_oversight(f) == 1.0
        assert oversight.oversight_reasons(f) == []


# ---------------------------------------------------------------------------
# 3. Approver concentration — a ceiling, not a floor
# ---------------------------------------------------------------------------

class TestConcentration:
    def test_single_approver_caps_the_score(self):
        f = facts(requested=10, approved=10, decided=10, expired=0,
                  lapse_rate=0.0, distinct_approvers=1)
        assert oversight.score_oversight(f) == oversight.CONCENTRATION_CEILING

    def test_concentration_is_a_cap_not_a_zero(self):
        """Concentrated oversight is a standing risk, not an outage."""
        f = facts(requested=10, approved=10, decided=10, expired=0,
                  lapse_rate=0.0, distinct_approvers=1)
        assert oversight.score_oversight(f) > 0.0

    def test_small_samples_do_not_trigger_concentration(self):
        """One approver across two decisions is a rota, not a bus factor."""
        n = oversight.MIN_DECISIONS_FOR_CONCENTRATION - 1
        f = facts(requested=n, approved=n, decided=n, expired=0,
                  lapse_rate=0.0, distinct_approvers=1)
        assert oversight.score_oversight(f) == 1.0

    def test_multiple_approvers_are_not_capped(self):
        f = facts(requested=10, approved=10, decided=10, expired=0,
                  lapse_rate=0.0, distinct_approvers=2)
        assert oversight.score_oversight(f) == 1.0

    def test_lapse_and_concentration_take_the_worse(self):
        """Both conditions → the lower number wins, not the average."""
        f = facts(requested=10, approved=5, decided=5, expired=5,
                  lapse_rate=0.5, distinct_approvers=1)
        assert oversight.score_oversight(f) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 4. What must stay UNSCORED
# ---------------------------------------------------------------------------

class TestDeliberatelyUnscored:
    def test_zero_denial_rate_does_not_reduce_score(self):
        """A well-behaved system is legitimately approved every time.

        Thresholding this would manufacture a finding for every system
        that simply never misbehaves.
        """
        f = facts(requested=20, approved=20, decided=20, expired=0,
                  lapse_rate=0.0, denial_rate=0.0, distinct_approvers=4)
        assert oversight.score_oversight(f) == 1.0

    def test_instant_decisions_do_not_reduce_score(self):
        """A fast approval may be an alert, well-staffed team."""
        f = facts(requested=20, approved=20, decided=20, expired=0,
                  lapse_rate=0.0, latency_p50_seconds=0.4,
                  distinct_approvers=4)
        assert oversight.score_oversight(f) == 1.0

    def test_report_declares_what_it_does_not_score(self):
        """The report must disclose the limit rather than imply coverage."""
        f = facts(requested=20, approved=20, decided=20,
                  lapse_rate=0.0, denial_rate=0.0,
                  latency_p50_seconds=0.4, distinct_approvers=4)
        section = oversight.oversight_report_section(f)
        assert set(section["not_scored"]) == {
            "denial_rate", "latency_p50_seconds",
        }
        assert "never thresholded" in section["not_scored_note"]

    def test_unscored_metrics_are_still_published(self):
        """Disclosed-not-scored still means visible to a human reader."""
        f = facts(requested=20, approved=18, rejected=2, decided=20,
                  lapse_rate=0.0, denial_rate=0.1,
                  latency_p50_seconds=42.0, distinct_approvers=4)
        section = oversight.oversight_report_section(f)
        assert section["denial_rate"] == 0.1
        assert section["latency_p50_seconds"] == 42.0

    def test_rubber_stamp_detection_is_delegated_to_drift(self):
        """The metric keys exist so drift learns them; no local threshold."""
        assert oversight.METRIC_APPROVAL_LATENCY_P50 in oversight.OVERSIGHT_METRIC_KEYS
        assert oversight.METRIC_APPROVAL_DENIAL_RATE in oversight.OVERSIGHT_METRIC_KEYS


# ---------------------------------------------------------------------------
# 5. Integration with the worst-of scorer
# ---------------------------------------------------------------------------

class TestScorerIntegration:
    def test_oversight_is_a_registered_component(self):
        assert "oversight" in COMPONENTS

    def test_oversight_can_be_the_worst_signal(self):
        """The whole point: bad oversight must be able to set the verdict."""
        result = score_health(
            liveness=1.0, behaviour=1.0, reliability=1.0,
            cost=1.0, conformance=1.0, oversight=0.4,
        )
        assert result["health_score"] == pytest.approx(0.4)
        assert result["health_state"] == "degraded"
        assert any("oversight" in r for r in result["reasons"])

    def test_oversight_does_not_mask_a_worse_signal(self):
        result = score_health(
            liveness=1.0, behaviour=0.2, reliability=1.0,
            cost=1.0, conformance=1.0, oversight=0.9,
        )
        assert result["health_score"] == pytest.approx(0.2)

    def test_oversight_score_is_surfaced_on_the_result(self):
        result = score_health(
            liveness=1.0, behaviour=1.0, reliability=1.0,
            cost=1.0, conformance=1.0, oversight=0.75,
        )
        assert result["oversight_score"] == 0.75

    def test_suspended_still_overrides_everything(self):
        """State ordering is unchanged by the new component."""
        result = score_health(
            liveness=1.0, behaviour=1.0, reliability=1.0,
            cost=1.0, conformance=1.0, oversight=0.1,
            is_suspended=True,
        )
        assert result["health_state"] == "suspended"


# ---------------------------------------------------------------------------
# 6. Latency arithmetic
# ---------------------------------------------------------------------------

class TestLatency:
    def _row(self, created, resolved):
        class _R:
            pass
        r = _R()
        r.created_at = created
        r.resolved_at = resolved
        return r

    def test_latency_computed_from_request_to_decision(self):
        now = datetime.now(timezone.utc)
        r = self._row(now, now + timedelta(seconds=90))
        assert oversight._latency_seconds(r) == pytest.approx(90.0)

    def test_unresolved_has_no_latency(self):
        now = datetime.now(timezone.utc)
        assert oversight._latency_seconds(self._row(now, None)) is None

    def test_negative_latency_is_dropped_not_propagated(self):
        """Clock skew between writers must not become a negative metric."""
        now = datetime.now(timezone.utc)
        r = self._row(now, now - timedelta(seconds=5))
        assert oversight._latency_seconds(r) is None


# ---------------------------------------------------------------------------
# 7. Empty-input safety
# ---------------------------------------------------------------------------

class TestGatherSafety:
    def test_no_group_ids_returns_empty_facts_without_querying(self):
        """Guards the caller that passes an empty descendant list."""
        got = oversight.gather_oversight(None, "t1", [], datetime.now(timezone.utc))
        assert got["requested"] == 0
        assert oversight.score_oversight(got) is None


# ---------------------------------------------------------------------------
# 8. One status vocabulary (app/constants/approvals.py)
# ---------------------------------------------------------------------------
#
# The bug class: a module classifies `tool_approval_requests.status` with its
# own literals and forgets one. `consumed` (approved, token since redeemed)
# was missing from both oversight counters, so every approval the agent
# actually carried out vanished from "decided by a human".

class TestOneStatusVocabulary:
    def _row(self, status, *, expires_in=3600, resolved_by="u1"):
        class _R:
            pass
        now = datetime.now(timezone.utc)
        r = _R()
        r.status = status
        r.created_at = now - timedelta(hours=2)
        r.expires_at = now + timedelta(seconds=expires_in)
        r.resolved_at = now - timedelta(hours=1)
        r.resolved_by = resolved_by
        return r

    def test_a_consumed_approval_is_an_approval(self):
        rows = [self._row("consumed"), self._row("consumed"), self._row("approved"), self._row("rejected")]
        got = oversight._counts(rows)
        assert (got["approved"], got["rejected"], got["decided"]) == (3, 1, 4)

    def test_every_status_lands_in_exactly_one_bucket(self):
        from app.constants import approvals as approval_status

        rows = [self._row(s) for s in approval_status.ALL_STATUSES]
        got = oversight._counts(rows)
        assert got["approved"] + got["rejected"] + got["expired"] + got["pending"] == got["requested"]

    def test_a_pending_hold_past_its_deadline_has_lapsed(self):
        got = oversight._counts([self._row("pending", expires_in=-60), self._row("pending")])
        assert (got["expired"], got["pending"]) == (1, 1)

    def test_no_module_classifies_approval_status_with_its_own_literals(self):
        import re
        from pathlib import Path

        from app.constants import approvals as approval_status

        app = Path(__file__).resolve().parents[1] / "app"
        words = "|".join(approval_status.ALL_STATUSES)
        literal = re.compile(rf"""(==|!=|\bin)\s*\(?\s*["']({words})["']""")
        found = []
        for path in python_sources(app):
            text = path.read_text()
            # Only code that handles approval requests; other tables have
            # their own "pending"/"approved".
            if "tool_approval_requests" not in text and "ToolApprovalRequest" not in text:
                continue
            if path.name == "approvals.py" and path.parent.name == "constants":
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if literal.search(line):
                    found.append(f"{source_label(path)}:{n}: {line.strip()}")
        assert not found, (
            "Classify approval requests with app.constants.approvals "
            "(decision / is_decided / is_lapsed), not with literals:\n  " + "\n  ".join(found)
        )
