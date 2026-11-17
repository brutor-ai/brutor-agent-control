# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Connection status — the rules, one test per rule

`connection_status` is the ONE function behind every connector line of the
Composition tab's Simple graph. Each rule, each threshold and each
resource type's health vocabulary is pinned here; the HTTP wiring (own rows
only, per-type traffic keys, dependency health reuse) is in
`tests/test_ai_system_composition.py::TestConnectionStatus`.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.database.models.ai_system_health import HEALTH_STATES
from app.services import ai_system_connection_status as cs
from app.services.ai_system_connection_status import (
    HealthSignal,
    TrafficEvidence,
    connection_status,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
WEEK = 168


def traffic(calls=0, errors=0, blocked=0, recent=(), held=0, held_lapsed=0):
    """`recent`: outcomes newest first ('ok' | 'error' | 'blocked' | 'held')."""
    return TrafficEvidence(
        calls=calls, errors=errors, blocked=blocked, held=held, held_lapsed=held_lapsed,
        last_call_at=NOW if calls else None,
        recent=[(NOW - timedelta(minutes=i), o) for i, o in enumerate(recent)],
    )


def status(t=None, signals=()):
    return connection_status(t, signals, window_hours=WEEK)


def test_thresholds_are_pinned():
    """Changing a threshold changes what every line on every system means —
    it must be a deliberate edit of this test too."""
    from app.services import ai_system_down_rule as rule

    assert rule.DOWN_AFTER_CONSECUTIVE_FAILURES == 3
    assert rule.DEGRADED_FAILURE_RATE == 0.05
    # Re-exported, never redefined (tests/test_down_rule_single_definition.py).
    assert cs.DOWN_AFTER_CONSECUTIVE_FAILURES is rule.DOWN_AFTER_CONSECUTIVE_FAILURES
    assert cs.DEGRADED_FAILURE_RATE is rule.DEGRADED_FAILURE_RATE
    assert cs.CONNECTION_STATUSES == ("ok", "degraded", "down", "idle")


class TestRules:
    def test_idle_no_calls_is_not_all_good(self):
        out = status()
        assert out == {"status": "idle", "reason": "No calls in the last 7 days",
                       "calls": 0, "errors": 0, "blocked": 0, "held": 0, "held_lapsed": 0,
                       "last_call_at": None}

    def test_ok_calls_and_nothing_wrong(self):
        out = status(traffic(12, recent=("ok", "ok", "ok")))
        assert (out["status"], out["reason"]) == ("ok", "12 calls, 0 errors")
        assert out["last_call_at"] == NOW.isoformat()

    def test_degraded_at_the_rate_threshold(self):
        # 1 of 20 = 5 % → degraded; 1 of 21 = 4.8 % → ok.
        out = status(traffic(20, errors=1, recent=("ok", "error", "ok")))
        assert (out["status"], out["reason"]) == ("degraded", "1 of 20 calls failed")
        assert status(traffic(21, errors=1, recent=("ok", "ok", "ok")))["status"] == "ok"

    def test_blocks_count_toward_the_rate_and_are_named(self):
        out = status(traffic(20, blocked=3, recent=("ok", "blocked", "ok")))
        assert (out["status"], out["reason"]) == ("degraded", "3 of 20 calls blocked by policy")
        mixed = status(traffic(20, errors=2, blocked=1, recent=("ok", "ok", "ok")))
        assert mixed["reason"] == "3 of 20 calls failed or were blocked"

    def test_down_when_the_last_three_calls_all_failed(self):
        out = status(traffic(100, errors=3, recent=("error", "error", "error")))
        assert (out["status"], out["reason"]) == ("down", "Last 3 calls failed")

    def test_down_when_the_last_three_calls_were_blocked(self):
        out = status(traffic(100, blocked=5, recent=("blocked", "blocked", "blocked")))
        assert (out["status"], out["reason"]) == ("down", "Last 3 calls blocked by policy")
        mixed = status(traffic(100, errors=1, blocked=2, recent=("blocked", "error", "blocked")))
        assert mixed["reason"] == "Last 3 calls failed or were blocked"

    def test_not_down_when_one_of_the_last_three_succeeded_or_fewer_than_three(self):
        # Two of the last three failed (2 % of the window): degraded, not down.
        out = status(traffic(100, errors=2, recent=("error", "ok", "error")))
        assert (out["status"], out["reason"]) == (
            "degraded", "2 of the last 3 calls failed · 100 calls, 2 errors")
        # Two calls, both failed: 100 % → degraded, but not N in a row.
        assert status(traffic(2, errors=2, recent=("error", "error")))["status"] == "degraded"

    def test_one_failure_among_the_latest_degrades_below_the_rate(self):
        """THE down rule (one fact, one computation): a single unanswered call among the
        latest is degraded — on every surface, whatever the window share."""
        out = status(traffic(100, errors=1, recent=("ok", "error", "ok")))
        assert (out["status"], out["reason"]) == (
            "degraded", "1 of the last 3 calls failed · 100 calls, 1 error")
        blocked = status(traffic(100, blocked=1, recent=("blocked", "ok", "ok")))
        assert blocked["reason"].startswith("1 of the last 3 calls blocked by policy")
        # Pushed out of the latest three by three good calls: ok again.
        assert status(traffic(100, errors=1, recent=("ok", "ok", "ok")))["status"] == "ok"

    def test_a_down_signal_overrides_good_traffic_and_keeps_the_evidence(self):
        out = status(traffic(12, recent=("ok", "ok", "ok")), [HealthSignal("down", "Model disabled")])
        assert (out["status"], out["reason"]) == ("down", "Model disabled · 12 calls, 0 errors")
        assert out["calls"] == 12

    def test_a_negative_signal_with_no_calls_is_not_idle(self):
        assert status(None, [HealthSignal("degraded", "Health degraded")]) == {
            "status": "degraded", "reason": "Health degraded · no calls in the last 7 days",
            "calls": 0, "errors": 0, "blocked": 0, "held": 0, "held_lapsed": 0,
            "last_call_at": None}
        assert status(None, [HealthSignal("down", "Skill archived")])["status"] == "down"

    def test_precedence_down_before_degraded(self):
        both = [HealthSignal("degraded", "Health degraded"), HealthSignal("down", "Disabled")]
        assert status(traffic(5, recent=("ok",)), both)["reason"].startswith("Disabled")
        stopped = traffic(10, errors=3, recent=("error", "error", "error"))
        assert status(stopped, [HealthSignal("degraded", "x")])["reason"] == "Last 3 calls failed"

    # -- Holds for human approval --------------------------------------------
    # A hold is the approval gate working, not the connection degrading. It
    # is counted on its own; only a hold nobody decided before its timeout
    # is a warning — and never more than a warning.

    def test_holds_are_counted_separately_not_as_degradation(self):
        out = status(traffic(32, held=7, recent=("held", "ok", "held")))
        assert (out["status"], out["reason"]) == ("ok", "32 calls, 0 errors, 7 held")
        assert (out["blocked"], out["held"], out["held_lapsed"]) == (0, 7, 0)

    def test_three_holds_in_a_row_are_not_down(self):
        out = status(traffic(3, held=3, recent=("held", "held", "held")))
        assert (out["status"], out["reason"]) == ("ok", "3 calls, 0 errors, 3 held")

    def test_a_hold_that_lapsed_without_a_decision_is_a_warning(self):
        out = status(traffic(32, held=7, held_lapsed=1, recent=("ok", "ok", "ok")))
        assert (out["status"], out["reason"]) == (
            "degraded", "1 hold expired without a decision · 32 calls, 0 errors, 7 held")
        assert out["held_lapsed"] == 1

    def test_lapsed_holds_never_make_a_line_down(self):
        out = status(traffic(3, held=3, held_lapsed=3, recent=("held", "held", "held")))
        assert (out["status"], out["reason"]) == (
            "degraded", "3 holds expired without a decision · 3 calls, 0 errors, 3 held")

    def test_holds_do_not_hide_failures(self):
        out = status(traffic(20, errors=1, held=5, recent=("held", "error", "ok")))
        assert (out["status"], out["reason"]) == ("degraded", "1 of 20 calls failed")
        down = status(traffic(20, errors=3, held=5, recent=("error", "error", "error")))
        assert down["status"] == "down"

    def test_evidence_sums_holds(self):
        a = traffic(2, held=1, held_lapsed=1)
        a.add(traffic(3, held=2))
        assert (a.calls, a.held, a.held_lapsed) == (5, 3, 1)

    def test_window_phrase(self):
        assert cs.window_phrase(168) == "the last 7 days"
        assert cs.window_phrase(24) == "the last day"
        assert cs.window_phrase(1) == "the last hour"
        assert cs.window_phrase(36) == "the last 36 hours"


def _model(**kw):
    base = dict(enabled=True, retire_date=None, health_status="healthy",
                health_check_error=None, consecutive_health_failures=0, provider_id="p")
    return SimpleNamespace(**{**base, **kw})


class TestHealthSignals:
    @pytest.mark.parametrize("model,provider,level,reason", [
        (_model(), None, None, None),
        (_model(health_status="unknown"), None, None, None),
        (_model(enabled=False), None, "down", "Model disabled"),
        (_model(), SimpleNamespace(enabled=False, display_name="OpenAI"), "down", "Provider OpenAI disabled"),
        (_model(retire_date=date(2026, 9, 1)), None, "down", f"Retired on {date(2026, 9, 1)}"),
        (_model(retire_date=date(2027, 1, 1)), None, None, None),
        (_model(health_status="unhealthy", health_check_error="Circuit breaker open"), None,
         "down", "Unhealthy: Circuit breaker open"),
        (_model(health_status="requires_tier"), None, "down",
         "Not available on the configured API key's tier"),
        (_model(health_status="degraded", consecutive_health_failures=2), None,
         "degraded", "Health degraded (2 consecutive failures)"),
    ])
    def test_models(self, model, provider, level, reason):
        signals = cs.model_signals(model, provider, today=date(2026, 9, 27))
        if level is None:
            assert signals == []
        else:
            assert (signals[0].level, signals[0].reason) == (level, reason)

    def test_mcp_servers(self):
        cfg, srv = SimpleNamespace(enabled=True), SimpleNamespace(
            enabled=True, health_status="healthy", health_check_error=None,
            consecutive_health_failures=0)
        assert cs.mcp_signals(cfg, srv) == []
        assert cs.mcp_signals(SimpleNamespace(enabled=False), srv)[0].reason == "Disabled for this tenant"
        srv.enabled = False
        assert cs.mcp_signals(cfg, srv)[0].reason == "MCP server disabled"
        srv.enabled, srv.health_status = True, "unhealthy"
        assert cs.mcp_signals(cfg, srv)[0].level == "down"
        srv.health_status = "degraded"
        assert cs.mcp_signals(cfg, srv)[0].level == "degraded"
        assert cs.mcp_signals(None, None)[0].level == "down"

    @pytest.mark.parametrize("enabled,state,level", [
        (True, "published", None), (False, "published", "down"),
        (True, "archived", "down"), (True, "deprecated", "degraded"),
    ])
    def test_skills(self, enabled, state, level):
        signals = cs.skill_signals(SimpleNamespace(enabled=enabled, status=state))
        assert (signals[0].level if signals else None) == level

    def test_knowledge_bases(self):
        assert cs.kb_signals(SimpleNamespace(enabled=True)) == []
        assert cs.kb_signals(SimpleNamespace(enabled=False))[0].reason == "Knowledge base disabled"
        assert cs.kb_signals(None)[0].level == "down"

    @pytest.mark.parametrize("health,signal,level", [
        ("healthy", None, None), ("idle", None, None), ("unknown", None, None),
        ("assumed", None, None),
        ("degraded", "liveness", "down"), ("degraded", "autonomy", "down"),
        ("degraded", "reliability", "degraded"), ("degraded", "dependency", "degraded"),
    ])
    def test_delegation_reads_the_dependency_status(self, health, signal, level):
        signals = cs.delegation_signals(health, signal)
        assert (signals[0].level if signals else None) == level


class TestSystemLine:
    @pytest.mark.parametrize("state", ["learning", "unknown", None])
    @pytest.mark.parametrize("t,expected", [
        (traffic(0), "idle"),
        (traffic(9, recent=("ok", "ok", "ok")), "ok"),
        (traffic(9, errors=1, recent=("ok", "error", "ok")), "degraded"),
        (traffic(9, errors=3, recent=("error", "error", "blocked")), "down"),
    ])
    def test_a_state_without_a_verdict_draws_the_line_from_its_own_traffic(self, state, t, expected):
        """Was: `learning` (baselines not ready) drew the grey "No calls"
        dash while the system was calling the gateway fine — the legend said
        "no calls in the window", which was false. A state that gives no
        verdict defers to the system's own traffic, by the same rule as every
        other line; only no calls at all is grey."""
        out = cs.system_connection(state, ["Still learning"], t, window_hours=WEEK)
        assert out["status"] == expected
        assert out["status"] == cs.connection_status(t, (), window_hours=WEEK)["status"]

    def test_every_stored_health_state_is_mapped(self):
        """A state added to the health vocabulary without a line colour
        would silently render idle — guard the whole vocabulary."""
        assert set(cs.SYSTEM_STATE_STATUS) == set(HEALTH_STATES)

    @pytest.mark.parametrize("state,expected", [
        ("healthy", "ok"), ("degraded", "degraded"), ("drifting", "degraded"),
        ("silent", "down"), ("suspended", "down"), ("stalled", "down"),
        # No verdict: the line is the system's own traffic by THE rule. Four
        # good calls are OK, not "no calls".
        ("learning", "ok"), ("unknown", "ok"), (None, "ok"),
    ])
    def test_mapping(self, state, expected):
        out = cs.system_connection(state, ["Reliability slipped"], traffic(4), window_hours=WEEK)
        assert out["status"] == expected
        assert out["calls"] == 4
        if state:
            assert out["reason"] == f"Health: {state} — Reliability slipped · 4 calls, 0 errors"
        else:
            assert out["reason"].startswith("No health computed yet")
