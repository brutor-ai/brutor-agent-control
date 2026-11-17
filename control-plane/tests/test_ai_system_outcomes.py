# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# THE run classification and the outcome catalogue (pure)

Hand-computed expectations, one case per cell of the classification table
and of the outcome catalogue's rules. The SQL form of the classification is pinned
row-for-row against the Python form over every cell.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import copy
import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest  # noqa: F401 — JSONB shim
from app.constants.outcomes import RUN_CLASSES, starter_catalogue
from app.database import Base
from app.database.models.ai_system_run import AISystemRun, TERMINAL_STATES
from app.services import ai_system_outcomes as oc
from app.services.ai_system_outcome_catalogue import (
    CatalogueValidationError,
    all_ids,
    changed_buckets,
    diff_catalogues,
    expect_violations,
    find,
    judge,
    merged_expect,
    validate_catalogue,
    validate_expected_outcomes,
)

# ---------------------------------------------------------------------------
# The classification table
# ---------------------------------------------------------------------------

STATES = ("completed", "completed_degraded", "errored", "exhausted", "abandoned", "cancelled")
#: The SQL parity table also holds rows the validator would refuse: an
#: unknown state (failed; terminal_state is NOT NULL, so no NULL row).
SQL_STATES = STATES + ("mystery_state",)
CONFORMANCES = ("declared", "undeclared", "unreported", None)
COUNTS = (True, False, None)


def _expected(state, conformance, counts) -> str:
    """The classification table, written out independently of the code."""
    if state in ("completed", "completed_degraded"):
        if conformance == "declared":
            return "success" if counts is True else "no_work"
        return "deviation"
    if state in ("abandoned", "cancelled", None):
        return "not_decisive"
    return "failed"  # errored, exhausted — and an unknown state


CELLS = list(itertools.product(STATES, CONFORMANCES, COUNTS))
SQL_CELLS = list(itertools.product(SQL_STATES, CONFORMANCES, COUNTS))


class TestClassificationTable:
    def test_states_are_the_full_vocabulary(self):
        assert set(STATES) == set(TERMINAL_STATES)
        assert "blocked_policy" not in TERMINAL_STATES

    @pytest.mark.parametrize("state,conformance,counts", CELLS)
    def test_every_cell(self, state, conformance, counts):
        assert oc.run_class(state, conformance, counts) == _expected(state, conformance, counts)

    @pytest.mark.parametrize("state,conformance,counts,cls", [
        ("completed", "declared", True, "success"),
        ("completed_degraded", "declared", True, "success"),
        ("completed", "declared", False, "no_work"),          # the empty poll
        ("completed", "undeclared", None, "deviation"),       # ended outside the contract
        ("completed", "unreported", None, "deviation"),       # said nothing
        ("errored", "declared", True, "failed"),              # errors are execution
        ("errored", "unreported", None, "failed"),
        ("exhausted", None, None, "failed"),
        ("abandoned", "declared", True, "not_decisive"),
        ("cancelled", None, None, "not_decisive"),
        (None, None, None, "not_decisive"),                   # open: never stored
        ("blocked_policy", None, None, "failed"),             # unknown ⇒ counts against
    ])
    def test_named_cells(self, state, conformance, counts, cls):
        assert oc.run_class(state, conformance, counts) == cls

    def test_run_class_of_reads_attributes(self):
        run = AISystemRun(terminal_state="completed", outcome_conformance="declared",
                          outcome_counts_toward_baseline=True)
        assert oc.run_class_of(run) == "success"
        assert run.run_class == "success" and run.is_successful is True
        run.outcome_counts_toward_baseline = False
        assert run.run_class == "no_work" and run.is_successful is False


def _rows(now):
    return [
        dict(id=f"r{i}", tenant_id="t1", root_task_id=f"root{i}", started_at=now,
             terminal_state=state, finalized_by="explicit", outcome_conformance=conformance,
             outcome_counts_toward_baseline=counts)
        for i, (state, conformance, counts) in enumerate(SQL_CELLS)
    ]


class TestSqlParity:
    """`run_class_sql` and `class_cond` give the same answer as `run_class`
    on every cell — the aggregations can never disagree with the rows."""

    @pytest.fixture(scope="class")
    def session(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool, future=True)
        Base.metadata.create_all(bind=engine, tables=[AISystemRun.__table__])
        s = sessionmaker(bind=engine, future=True)()
        now = datetime.now(timezone.utc)
        # A core insert: the ORM validator would refuse the unknown state.
        s.execute(AISystemRun.__table__.insert(), _rows(now))
        s.commit()
        yield s
        s.close()

    def test_case_expression_matches_python(self, session):
        rows = session.query(AISystemRun.id, oc.run_class_sql()).all()
        assert len(rows) == len(SQL_CELLS)
        for rid, cls in rows:
            state, conformance, counts = SQL_CELLS[int(rid[1:])]
            assert cls == _expected(state, conformance, counts), (rid, state, conformance, counts)

    @pytest.mark.parametrize("cls", RUN_CLASSES)
    def test_class_conditions_partition_the_rows(self, session, cls):
        ids = {r for (r,) in session.query(AISystemRun.id).filter(oc.class_cond(cls))}
        expected = {f"r{i}" for i, cell in enumerate(SQL_CELLS) if _expected(*cell) == cls}
        assert ids == expected

    def test_rate_and_waste_conditions(self, session):
        rate = {r for (r,) in session.query(AISystemRun.id).filter(oc.rate_cond())}
        waste = {r for (r,) in session.query(AISystemRun.id).filter(oc.waste_cond())}
        assert rate == {f"r{i}" for i, c in enumerate(SQL_CELLS)
                        if _expected(*c) in ("success", "deviation", "failed")}
        assert waste == {f"r{i}" for i, c in enumerate(SQL_CELLS)
                         if _expected(*c) in ("deviation", "failed")}

    def test_unknown_class_is_refused(self):
        with pytest.raises(ValueError):
            oc.class_cond("blocked")


class TestSuccessRate:
    def test_formula(self):
        # 6 success, 10 no_work, 2 deviation, 2 failed, 5 not_decisive
        # → 6 / (6 + 2 + 2) = 0.6; no_work and not_decisive in neither.
        counts = {"success": 6, "no_work": 10, "deviation": 2, "failed": 2, "not_decisive": 5}
        assert oc.success_rate(counts) == pytest.approx(0.6)

    def test_empty_polls_do_not_inflate_it(self):
        # An outage: 40 polls succeed, 8 screenings error.
        assert oc.success_rate({"no_work": 40, "failed": 8}) == 0.0

    def test_no_denominator_is_none(self):
        assert oc.success_rate({"no_work": 20, "not_decisive": 3}) is None
        assert oc.success_rate({}) is None

    def test_class_counts_has_every_class(self):
        assert oc.class_counts(["success", "success", "failed"]) == {
            "success": 2, "no_work": 0, "deviation": 0, "failed": 1, "not_decisive": 0,
        }

    @pytest.mark.parametrize("cls,expected", [
        ("success", False), ("deviation", True), ("failed", True),
        ("no_work", None), ("not_decisive", None),
    ])
    def test_down_rule_evidence(self, cls, expected):
        assert oc.down_rule_failed(cls) is expected
        assert oc.in_rate(cls) is (expected is not None)

    def test_split_outcome_id(self):
        assert oc.split_outcome_id("a.b") == ("a", "b")
        assert oc.split_outcome_id("a") == ("a", None)
        assert oc.split_outcome_id(None) == (None, None)
        assert oc.outcome_of("application_processed.loan_approved") == "application_processed"


# ---------------------------------------------------------------------------
# The catalogue — validation
# ---------------------------------------------------------------------------

DEMO = [
    {"id": "no_applications_available", "label": "Nothing to screen",
     "counts_toward_baseline": False,
     "expect": {"max_model_calls": 0, "max_cost_usd": 0,
                "forbids_capabilities": ["applications_set_recommendation",
                                         "applications_add_note", "applications_hand_off"]}},
    {"id": "application_processed", "label": "Application processed",
     "counts_toward_baseline": True,
     "variants": [
         {"id": "loan_rejected", "label": "Decline",
          "expect": {"requires_capabilities": ["applications_set_recommendation"]}},
         {"id": "loan_approved", "label": "Approve",
          "expect": {"requires_capabilities": ["applications_set_recommendation"]}},
         {"id": "underwriter_decision_applied", "label": "Applied",
          "expect": {"max_actions": 2, "max_model_calls": 0}},
     ]},
]


def _errors(raw):
    with pytest.raises(CatalogueValidationError) as exc:
        validate_catalogue(raw)
    return exc.value.errors


class TestValidation:
    def test_normal_form_is_sorted(self):
        norm = validate_catalogue(DEMO)
        assert [o["id"] for o in norm] == ["application_processed", "no_applications_available"]
        assert [v["id"] for v in norm[0]["variants"]] == [
            "loan_approved", "loan_rejected", "underwriter_decision_applied",
        ]
        assert norm[1]["expect"]["forbids_capabilities"] == [
            "applications_add_note", "applications_hand_off", "applications_set_recommendation",
        ]
        assert norm[1]["expect"]["max_cost_usd"] == 0 and isinstance(norm[1]["expect"]["max_cost_usd"], int)

    def test_idempotent_and_order_independent(self):
        once = validate_catalogue(DEMO)
        assert validate_catalogue(once) == once
        assert validate_catalogue(list(reversed(copy.deepcopy(DEMO)))) == once

    def test_starter_catalogue_is_valid(self):
        norm = validate_catalogue(starter_catalogue())
        assert [o["id"] for o in norm] == ["nothing_to_do", "task_completed"]
        assert norm[0]["counts_toward_baseline"] is False
        assert norm[0]["expect"] == {"max_actions": 2, "max_cost_usd": 0, "max_model_calls": 0}
        assert norm[1]["counts_toward_baseline"] is True

    def test_empty_is_none(self):
        assert validate_catalogue(None) is None
        assert validate_catalogue([]) is None

    def test_descriptions_kept_trimmed_and_blank_dropped(self):
        norm = validate_catalogue([{"id": "a", "label": " A ", "description": "  why  ",
                                    "counts_toward_baseline": True},
                                   {"id": "b", "label": "B", "description": "   ",
                                    "counts_toward_baseline": True}])
        assert norm[0] == {"id": "a", "label": "A", "description": "why", "counts_toward_baseline": True}
        assert "description" not in norm[1]

    @pytest.mark.parametrize("raw,loc,needle", [
        ({"id": "a"}, ["outcomes"], "list"),
        (["x"], ["outcomes", 0], "object"),
        ([{"id": "A!", "label": "x", "counts_toward_baseline": True}], ["outcomes", 0, "id"], "[a-z0-9_-]"),
        ([{"id": "a" * 49, "label": "x", "counts_toward_baseline": True}], ["outcomes", 0, "id"], "[a-z0-9_-]"),
        ([{"id": "a", "label": "", "counts_toward_baseline": True}], ["outcomes", 0, "label"], "label"),
        ([{"id": "a", "label": "x" * 129, "counts_toward_baseline": True}], ["outcomes", 0, "label"], "128"),
        ([{"id": "a", "label": "x"}], ["outcomes", 0, "counts_toward_baseline"], "true or false"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": "yes"}], ["outcomes", 0, "counts_toward_baseline"], "true or false"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "colour": "red"}], ["outcomes", 0, "colour"], "unknown key"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "description": 3}], ["outcomes", 0, "description"], "string"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True},
          {"id": "a", "label": "y", "counts_toward_baseline": True}], ["outcomes", 1, "id"], "duplicate"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": []}], ["outcomes", 0, "expect"], "object"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"max_tokens": 3}}],
         ["outcomes", 0, "expect", "max_tokens"], "unknown key"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"max_actions": -1}}],
         ["outcomes", 0, "expect", "max_actions"], "≥ 0"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"max_actions": 1.5}}],
         ["outcomes", 0, "expect", "max_actions"], "whole"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"max_model_calls": True}}],
         ["outcomes", 0, "expect", "max_model_calls"], "whole"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"max_cost_usd": -0.01}}],
         ["outcomes", 0, "expect", "max_cost_usd"], "≥ 0"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"requires_capabilities": "x"}}],
         ["outcomes", 0, "expect", "requires_capabilities"], "list"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "expect": {"forbids_capabilities": [""]}}],
         ["outcomes", 0, "expect", "forbids_capabilities"], "list"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True,
           "expect": {"requires_capabilities": ["c"], "forbids_capabilities": ["c"]}}],
         ["outcomes", 0, "expect"], "both required and forbidden"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "variants": {}}],
         ["outcomes", 0, "variants"], "list"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True, "variants": ["v"]}],
         ["outcomes", 0, "variants", 0], "object"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True,
           "variants": [{"id": "v", "label": "V"}, {"id": "v", "label": "W"}]}],
         ["outcomes", 0, "variants", 1, "id"], "duplicate"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True,
           "variants": [{"id": "v.w", "label": "V"}]}], ["outcomes", 0, "variants", 0, "id"], "[a-z0-9_-]"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True,
           "variants": [{"id": "v", "label": "V", "counts_toward_baseline": True}]}],
         ["outcomes", 0, "variants", 0, "counts_toward_baseline"], "unknown key"),
        ([{"id": "a", "label": "x", "counts_toward_baseline": True,
           "variants": [{"id": "v", "label": "V", "expect": {"max_actions": "2"}}]}],
         ["outcomes", 0, "variants", 0, "expect", "max_actions"], "whole"),
    ])
    def test_every_422_path(self, raw, loc, needle):
        errors = _errors(raw)
        assert any(e["loc"] == loc and needle in e["msg"] for e in errors), errors

    def test_limits(self):
        many = [{"id": f"o{i}", "label": "x", "counts_toward_baseline": True} for i in range(33)]
        assert any("at most 32 outcomes" in e["msg"] for e in _errors(many))
        variants = [{"id": f"v{i}", "label": "x"} for i in range(33)]
        assert any("at most 32 variants" in e["msg"] for e in _errors(
            [{"id": "a", "label": "x", "counts_toward_baseline": True, "variants": variants}]))
        ok = [{"id": f"o{i}", "label": "x", "counts_toward_baseline": True} for i in range(32)]
        assert len(validate_catalogue(ok)) == 32

    def test_every_error_is_reported_at_once(self):
        errors = _errors([{"id": "A", "label": ""}, {"id": "b", "label": "x", "counts_toward_baseline": 1}])
        assert len(errors) == 4

    def test_message_names_the_paths(self):
        with pytest.raises(CatalogueValidationError) as exc:
            validate_catalogue([{"id": "a", "label": "x"}])
        assert "outcomes.0.counts_toward_baseline" in str(exc.value)


class TestExpectedOutcomes:
    def test_normalised(self):
        assert validate_expected_outcomes(None) is None
        assert validate_expected_outcomes(["b.x", "a", "b.x"]) == ["a", "b.x"]

    @pytest.mark.parametrize("raw", ["a", [1], ["A"], ["a.b.c"], ["a."]])
    def test_refused(self, raw):
        with pytest.raises(ValueError):
            validate_expected_outcomes(raw)


# ---------------------------------------------------------------------------
# Judging, evidence
# ---------------------------------------------------------------------------

CAT = validate_catalogue(DEMO)


class TestJudge:
    @pytest.mark.parametrize("reported,expected", [
        (None, ("unreported", None)),
        ("", ("unreported", None)),
        ("no_applications_available", ("declared", False)),
        ("application_processed.loan_approved", ("declared", True)),
        ("application_processed", ("undeclared", None)),            # variant required
        ("application_processed.loan_maybe", ("undeclared", None)),
        ("no_applications_available.x", ("undeclared", None)),      # no variants declared
        ("made_up", ("undeclared", None)),
        ("Not Valid!", ("undeclared", None)),                       # malformed
    ])
    def test_cells(self, reported, expected):
        assert judge(CAT, reported) == expected

    def test_no_catalogue_is_undeclared(self):
        assert judge(None, "task_completed") == ("undeclared", None)

    def test_all_ids(self):
        assert all_ids(CAT) == [
            "application_processed.loan_approved",
            "application_processed.loan_rejected",
            "application_processed.underwriter_decision_applied",
            "no_applications_available",
        ]
        assert find(CAT, "nope") == (None, None)


class TestEvidence:
    def test_variant_keys_win_over_the_outcome(self):
        cat = validate_catalogue([{
            "id": "a", "label": "A", "counts_toward_baseline": True,
            "expect": {"max_actions": 10, "max_model_calls": 3},
            "variants": [{"id": "v", "label": "V", "expect": {"max_actions": 2}},
                         {"id": "w", "label": "W"}],
        }])
        assert merged_expect(cat, "a.v") == {"max_actions": 2, "max_model_calls": 3}
        assert merged_expect(cat, "a.w") == {"max_actions": 10, "max_model_calls": 3}
        assert merged_expect(cat, "a.x") is None

    def test_a_poll_with_a_model_call_breaks_its_evidence(self):
        exp = merged_expect(CAT, "no_applications_available")
        broken = expect_violations(exp, action_count=1, llm_call_count=1, total_cost_usd=0.002,
                                   capabilities=[])
        assert broken == [
            "1 model calls (expected at most 0)",
            "cost $0.0020 (expected at most $0.0000)",
        ]

    def test_a_clean_poll_meets_it(self):
        exp = merged_expect(CAT, "no_applications_available")
        assert expect_violations(exp, action_count=25, llm_call_count=0, total_cost_usd=0,
                                 capabilities=["applications_list_pending"]) == []

    def test_capabilities(self):
        exp = merged_expect(CAT, "application_processed.loan_approved")
        assert expect_violations(exp, action_count=10, llm_call_count=3, total_cost_usd=0.02,
                                 capabilities=[]) == [
            "required capabilities not used: ['applications_set_recommendation']"
        ]
        poll = merged_expect(CAT, "no_applications_available")
        assert expect_violations(poll, action_count=1, llm_call_count=0, total_cost_usd=0,
                                 capabilities=["applications_add_note"]) == [
            "forbidden capabilities used: ['applications_add_note']"
        ]

    def test_max_actions(self):
        exp = merged_expect(CAT, "application_processed.underwriter_decision_applied")
        assert expect_violations(exp, action_count=3, llm_call_count=0, total_cost_usd=0,
                                 capabilities=[]) == ["3 actions (expected at most 2)"]
        assert expect_violations(None, action_count=99, llm_call_count=9, total_cost_usd=9,
                                 capabilities=[]) == []


# ---------------------------------------------------------------------------
# Contract diff and catalogue-change buckets
# ---------------------------------------------------------------------------


def _diff(before, after):
    w, n, c = [], [], []
    diff_catalogues(validate_catalogue(before), validate_catalogue(after), w, n, c)
    return w, n, c


def _base(**over):
    o = {"id": "a", "label": "A", "counts_toward_baseline": True,
         "expect": {"max_actions": 5, "requires_capabilities": ["r"], "forbids_capabilities": ["f"]},
         "variants": [{"id": "v", "label": "V", "expect": {"max_cost_usd": 1}}]}
    o.update(over)
    return [o]


class TestDiff:
    def test_identical_is_empty(self):
        assert _diff(_base(), _base()) == ([], [], [])

    def test_add_outcome_widens_remove_narrows(self):
        extra = _base() + [{"id": "b", "label": "B", "counts_toward_baseline": False}]
        w, n, _ = _diff(_base(), extra)
        assert [e["kind"] for e in w] == ["outcome_added"] and n == []
        assert "does NOT count" in w[0]["detail"]
        w, n, _ = _diff(extra, _base())
        assert w == [] and [e["kind"] for e in n] == ["outcome_removed"]
        w, _, _ = _diff(None, _base())
        assert [e["kind"] for e in w] == ["outcome_added"]

    def test_add_variant_widens_remove_narrows(self):
        more = _base(variants=[{"id": "v", "label": "V", "expect": {"max_cost_usd": 1}},
                               {"id": "x", "label": "X"}])
        w, n, _ = _diff(_base(), more)
        assert [(e["kind"], e["id"]) for e in w] == [("outcome_variant_added", "a.x")] and n == []
        w, n, _ = _diff(more, _base())
        assert w == [] and [(e["kind"], e["id"]) for e in n] == [("outcome_variant_removed", "a.x")]

    def test_baseline_flag_true_to_false_widens_and_is_called_out(self):
        w, n, _ = _diff(_base(), _base(counts_toward_baseline=False))
        assert [e["kind"] for e in w] == ["outcome_baseline_flag"] and n == []
        assert w[0]["called_out"] is True and (w[0]["from"], w[0]["to"]) == (True, False)
        w, n, _ = _diff(_base(counts_toward_baseline=False), _base())
        assert w == [] and [e["kind"] for e in n] == ["outcome_baseline_flag"]

    @pytest.mark.parametrize("after_expect,direction,field", [
        ({"max_actions": 9, "requires_capabilities": ["r"], "forbids_capabilities": ["f"]}, "widened", "max_actions"),
        ({"max_actions": 3, "requires_capabilities": ["r"], "forbids_capabilities": ["f"]}, "narrowed", "max_actions"),
        ({"requires_capabilities": ["r"], "forbids_capabilities": ["f"]}, "widened", "max_actions"),
        ({"max_actions": 5, "max_model_calls": 0, "requires_capabilities": ["r"],
          "forbids_capabilities": ["f"]}, "narrowed", "max_model_calls"),
        ({"max_actions": 5, "forbids_capabilities": ["f"]}, "widened", "requires_capabilities"),
        ({"max_actions": 5, "requires_capabilities": ["r", "s"], "forbids_capabilities": ["f"]},
         "narrowed", "requires_capabilities"),
        ({"max_actions": 5, "requires_capabilities": ["r"]}, "widened", "forbids_capabilities"),
        ({"max_actions": 5, "requires_capabilities": ["r"], "forbids_capabilities": ["f", "g"]},
         "narrowed", "forbids_capabilities"),
    ])
    def test_expect_direction(self, after_expect, direction, field):
        w, n, c = _diff(_base(), _base(expect=after_expect))
        hit = w if direction == "widened" else n
        other = n if direction == "widened" else w
        assert [e["field"] for e in hit] == [field] and other == [] and c == []

    def test_variant_expect_loosened(self):
        w, n, _ = _diff(_base(), _base(variants=[{"id": "v", "label": "V", "expect": {"max_cost_usd": 2}}]))
        assert [(e["kind"], e["id"]) for e in w] == [("outcome_expect_loosened", "a.v")] and n == []

    def test_label_and_description_are_neither(self):
        w, n, c = _diff(_base(), _base(label="Renamed", description="why",
                                       variants=[{"id": "v", "label": "V2", "expect": {"max_cost_usd": 1}}]))
        assert w == [] and n == []
        assert sorted((e["id"], e["field"]) for e in c) == [
            ("a", "description"), ("a", "label"), ("a.v", "label"),
        ]


class TestChangedBuckets:
    def test_unchanged(self):
        assert changed_buckets(validate_catalogue(_base()), validate_catalogue(_base())) == ([], False)

    def test_outcome_expect_changes_every_variant_bucket(self):
        before = validate_catalogue(_base(variants=[{"id": "v", "label": "V"}, {"id": "w", "label": "W"}]))
        after = validate_catalogue(_base(expect={"max_actions": 6},
                                         variants=[{"id": "v", "label": "V"}, {"id": "w", "label": "W"}]))
        assert changed_buckets(before, after) == (["a.v", "a.w"], False)

    def test_variant_expect_changes_its_bucket_only(self):
        before = validate_catalogue(_base(variants=[{"id": "v", "label": "V"}, {"id": "w", "label": "W"}]))
        after = validate_catalogue(_base(variants=[{"id": "v", "label": "V", "expect": {"max_actions": 1}},
                                                   {"id": "w", "label": "W"}]))
        assert changed_buckets(before, after) == (["a.v"], False)

    def test_flag_change(self):
        assert changed_buckets(validate_catalogue(_base()),
                               validate_catalogue(_base(counts_toward_baseline=False))) == (["a.v"], True)

    def test_added_or_removed_buckets_need_no_epoch(self):
        before = validate_catalogue(_base())
        after = validate_catalogue(_base(variants=[{"id": "v", "label": "V", "expect": {"max_cost_usd": 1}},
                                                   {"id": "x", "label": "X"}]))
        assert changed_buckets(before, after) == ([], False)
        assert changed_buckets(after, before) == ([], False)
        assert changed_buckets(None, before) == ([], False)

    def test_label_change_is_no_bucket_change(self):
        assert changed_buckets(validate_catalogue(_base()),
                               validate_catalogue(_base(label="Other"))) == ([], False)


class TestSqlNegationIsNullSafe:
    """Three-valued logic: `NOT (no_work)` must keep a completed run whose
    conformance is NULL (tokenomics excludes no-work runs this way)."""

    def test_negations_partition(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool, future=True)
        Base.metadata.create_all(bind=engine, tables=[AISystemRun.__table__])
        s = sessionmaker(bind=engine, future=True)()
        now = datetime.now(timezone.utc)
        s.execute(AISystemRun.__table__.insert(), _rows(now))
        s.commit()
        for cls in RUN_CLASSES:
            inside = s.query(AISystemRun).filter(oc.class_cond(cls)).count()
            outside = s.query(AISystemRun).filter(~oc.class_cond(cls)).count()
            assert inside + outside == len(SQL_CELLS), cls
        s.close()
