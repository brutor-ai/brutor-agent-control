# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for AI System evidence attachments (a146_ai_system_evidence) and the
high-risk impact-assessment floor on the lifecycle gate.

What is worth defending:

  * **A row is a pointer with a fingerprint.** kind/sha256 are validated,
    the sha is normalized, expiry is computed — a reviewer must be able to
    trust what the row claims about the document.
  * **The gate floor is real.** A high-risk system cannot pass
    `proposed → approved` without an unexpired `impact_assessment`; a
    tenant policy cannot remove that; lower tiers are not burdened with it.
  * **Expired is not absent.** The two refusals read differently, because
    the fix is different.
  * **Only the right kind satisfies.** An eval report is not an impact
    assessment.
  * **Tenant isolation everywhere.**
  * **The report stays honest.** `not_covered` names the missing or
    self-produced evidence; attaching independent evidence removes the line.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.ai_system_contracts import (
    create_ai_system_contracts_router,
)
from app.api.v1.endpoints.admin.ai_system_evidence import (
    create_ai_system_evidence_router,
)
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services import ai_system_lifecycle_gate as gate
from app.services.ai_system_assurance import _not_covered


@pytest.fixture()
def h():
    log = BrutorLogger("test-ai-system-evidence")
    harness = build_admin_harness(
        [
            create_ai_system_evidence_router(log),
            create_ai_system_contracts_router(log),
        ],
        logger_name="test-ai-system-evidence",
    )
    yield harness
    harness.close()


def _add_system(h, gid, tenant="t1", **kwargs):
    def fn(s):
        s.add(
            ResourceGroup(
                id=gid,
                tenant_id=tenant,
                name=gid,
                hierarchy_level=0,
                inherit_resources=True,
                is_active=True,
                is_system=False,
                group_type=GroupType.AI_SYSTEM,
                system_kind="agent",
                **kwargs,
            )
        )

    h.seed(fn)
    return gid


def _attach(h, sid, tenant="t1", **over):
    body = {"kind": "impact_assessment", "title": "FRIA 2026"}
    body.update(over)
    return h.client.post(
        f"/v1/admin/ai-systems/{sid}/evidence", json=body, headers=h.headers(tenant)
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    """The wall clock, read when used — never at import (tests/test_no_import_time_clock.py)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# CRUD + validation
# ---------------------------------------------------------------------------


class TestCrud:
    def test_attach_round_trips_every_field(self, h):
        sid = _add_system(h, "sys-crud")
        r = _attach(
            h,
            sid,
            notes="annual FRIA",
            uri="https://docs.example.com/fria-2026.pdf",
            sha256="AB" * 32,  # uppercase on purpose — must normalize
            assessor="Nordic AI Audit AB",
            independent=True,
            produced_at=_iso(_now() - timedelta(days=10)),
            valid_until=_iso(_now() + timedelta(days=355)),
        )
        assert r.status_code == 201, r.text
        e = r.json()
        assert e["kind"] == "impact_assessment"
        assert e["title"] == "FRIA 2026"
        assert e["sha256"] == "ab" * 32
        assert e["assessor"] == "Nordic AI Audit AB"
        assert e["independent"] is True
        assert e["is_expired"] is False
        assert e["id"].startswith("ev-")

    def test_list_and_kind_filter(self, h):
        sid = _add_system(h, "sys-list")
        assert _attach(h, sid).status_code == 201
        assert _attach(h, sid, kind="eval_report", title="Q3 evals").status_code == 201

        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/evidence", headers=h.headers()
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 2

        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/evidence",
            params={"kind": "eval_report"},
            headers=h.headers(),
        )
        assert r.status_code == 200
        assert [e["kind"] for e in r.json()["evidence"]] == ["eval_report"]

    def test_list_with_unknown_kind_is_422(self, h):
        sid = _add_system(h, "sys-badkindq")
        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/evidence",
            params={"kind": "vibes"},
            headers=h.headers(),
        )
        assert r.status_code == 422

    def test_patch_updates_metadata(self, h):
        sid = _add_system(h, "sys-patch")
        eid = _attach(h, sid).json()["id"]
        r = h.client.patch(
            f"/v1/admin/ai-systems/{sid}/evidence/{eid}",
            json={"title": "FRIA 2026 (renewed)", "independent": False},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "FRIA 2026 (renewed)"
        assert r.json()["independent"] is False

    def test_delete_detaches(self, h):
        sid = _add_system(h, "sys-del")
        eid = _attach(h, sid).json()["id"]
        r = h.client.delete(
            f"/v1/admin/ai-systems/{sid}/evidence/{eid}", headers=h.headers()
        )
        assert r.status_code == 200, r.text
        listing = h.client.get(
            f"/v1/admin/ai-systems/{sid}/evidence", headers=h.headers()
        )
        assert listing.json()["total"] == 0

    def test_unknown_kind_is_422(self, h):
        sid = _add_system(h, "sys-badkind")
        r = _attach(h, sid, kind="astrology_reading")
        assert r.status_code == 422, r.text

    def test_malformed_sha256_is_422(self, h):
        sid = _add_system(h, "sys-badsha")
        r = _attach(h, sid, sha256="not-a-hash")
        assert r.status_code == 422, r.text

    def test_valid_until_before_produced_at_is_422(self, h):
        sid = _add_system(h, "sys-baddates")
        r = _attach(
            h,
            sid,
            produced_at=_iso(_now()),
            valid_until=_iso(_now() - timedelta(days=1)),
        )
        assert r.status_code == 422, r.text

    def test_expired_row_reports_is_expired(self, h):
        sid = _add_system(h, "sys-expired")
        r = _attach(
            h,
            sid,
            produced_at=_iso(_now() - timedelta(days=400)),
            valid_until=_iso(_now() - timedelta(days=35)),
        )
        assert r.status_code == 201
        assert r.json()["is_expired"] is True

    def test_plain_access_group_is_422(self, h):
        def fn(s):
            s.add(
                ResourceGroup(
                    id="grp-plain",
                    tenant_id="t1",
                    name="grp-plain",
                    hierarchy_level=0,
                    inherit_resources=True,
                    is_active=True,
                    is_system=False,
                    group_type=GroupType.CUSTOM,
                )
            )

        h.seed(fn)
        r = _attach(h, "grp-plain")
        assert r.status_code == 422, r.text

    def test_unknown_system_is_404(self, h):
        assert _attach(h, "sys-nope").status_code == 404

    def test_unknown_evidence_id_is_404(self, h):
        sid = _add_system(h, "sys-404ev")
        r = h.client.patch(
            f"/v1/admin/ai-systems/{sid}/evidence/ev-nope",
            json={"title": "x"},
            headers=h.headers(),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_other_tenant_cannot_see_touch_or_attach(self, h):
        sid = _add_system(h, "sys-iso", tenant="t1")
        eid = _attach(h, sid).json()["id"]

        assert (
            h.client.get(
                f"/v1/admin/ai-systems/{sid}/evidence", headers=h.headers("t2")
            ).status_code
            == 404
        )
        assert (
            h.client.patch(
                f"/v1/admin/ai-systems/{sid}/evidence/{eid}",
                json={"title": "hostile"},
                headers=h.headers("t2"),
            ).status_code
            == 404
        )
        assert (
            h.client.delete(
                f"/v1/admin/ai-systems/{sid}/evidence/{eid}",
                headers=h.headers("t2"),
            ).status_code
            == 404
        )
        assert _attach(h, sid, tenant="t2").status_code == 404


# ---------------------------------------------------------------------------
# The lifecycle gate floor
# ---------------------------------------------------------------------------


def _approved_requirements(h, sid, tenant="t1"):
    r = h.client.get(
        f"/v1/admin/ai-systems/{sid}/lifecycle", headers=h.headers(tenant)
    )
    assert r.status_code == 200, r.text
    opt = next(
        t for t in r.json()["transitions"] if t["to_stage"] == "approved"
    )
    return {req["key"]: req for req in opt["requirements"]}


class TestGateFloor:
    def test_low_risk_systems_are_not_burdened(self, h):
        sid = _add_system(h, "sys-low", eu_ai_act_risk_tier="minimal")
        reqs = _approved_requirements(h, sid)
        assert gate.REQ_IMPACT_ASSESSMENT not in reqs

    def test_high_risk_requires_it_and_refuses_without(self, h):
        sid = _add_system(h, "sys-high", eu_ai_act_risk_tier="high")
        reqs = _approved_requirements(h, sid)
        assert gate.REQ_IMPACT_ASSESSMENT in reqs
        assert reqs[gate.REQ_IMPACT_ASSESSMENT]["satisfied"] is False

        r = h.client.post(
            f"/v1/admin/ai-systems/{sid}/lifecycle",
            json={"to_stage": "approved", "approved_by": "cto"},
            headers=h.headers(),
        )
        assert r.status_code == 409, r.text
        unmet = {
            u["key"] for u in r.json()["detail"]["decision"]["unmet"]
        }
        assert gate.REQ_IMPACT_ASSESSMENT in unmet

    def test_attaching_an_assessment_satisfies_the_floor(self, h):
        sid = _add_system(h, "sys-high-ok", eu_ai_act_risk_tier="high")
        assert _attach(h, sid).status_code == 201
        reqs = _approved_requirements(h, sid)
        assert reqs[gate.REQ_IMPACT_ASSESSMENT]["satisfied"] is True

    def test_an_expired_assessment_does_not_and_says_expired(self, h):
        sid = _add_system(h, "sys-high-exp", eu_ai_act_risk_tier="high")
        assert (
            _attach(
                h,
                sid,
                valid_until=_iso(_now() - timedelta(days=3)),
            ).status_code
            == 201
        )
        reqs = _approved_requirements(h, sid)
        req = reqs[gate.REQ_IMPACT_ASSESSMENT]
        assert req["satisfied"] is False
        assert "expired" in (req["detail"] or "").lower()

    def test_an_eval_report_is_not_an_impact_assessment(self, h):
        sid = _add_system(h, "sys-high-eval", eu_ai_act_risk_tier="high")
        assert (
            _attach(h, sid, kind="eval_report", title="Q3 evals").status_code == 201
        )
        reqs = _approved_requirements(h, sid)
        assert reqs[gate.REQ_IMPACT_ASSESSMENT]["satisfied"] is False

    def test_only_the_approval_step_carries_the_floor(self, h):
        """`approved → active` already demands approver/liveness/replay; the
        document was read at approval and does not get truer by being
        checked twice."""
        sid = _add_system(
            h,
            "sys-high-active",
            eu_ai_act_risk_tier="high",
            lifecycle_stage="approved",
        )
        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/lifecycle", headers=h.headers()
        )
        opt = next(
            t for t in r.json()["transitions"] if t["to_stage"] == "active"
        )
        assert gate.REQ_IMPACT_ASSESSMENT not in {
            req["key"] for req in opt["requirements"]
        }


class TestGateFloorSemantics:
    """Pure-function checks on required_keys — floor and opt-in semantics."""

    def _system(self, tier=None, stage=None):
        return ResourceGroup(
            id="s",
            tenant_id="t1",
            name="s",
            hierarchy_level=0,
            group_type=GroupType.AI_SYSTEM,
            eu_ai_act_risk_tier=tier,
            lifecycle_stage=stage,
        )

    def test_tenant_policy_cannot_remove_the_floor(self, h):
        keys = gate.required_keys(
            self._system(tier="high"),
            "approved",
            tenant_policy={"proposed->approved": ["owner_set"]},
        )
        assert gate.REQ_IMPACT_ASSESSMENT in keys
        assert gate.REQ_HUMAN_APPROVAL in keys

    def test_tenant_policy_can_opt_lower_tiers_in(self, h):
        keys = gate.required_keys(
            self._system(tier="limited"),
            "approved",
            tenant_policy={
                "proposed->approved": [
                    "owner_set",
                    gate.REQ_IMPACT_ASSESSMENT,
                ]
            },
        )
        assert gate.REQ_IMPACT_ASSESSMENT in keys

    def test_the_label_reads_like_a_sentence(self, h):
        label = gate.REQUIREMENT_LABELS[gate.REQ_IMPACT_ASSESSMENT]
        assert "impact assessment" in label.lower()


# ---------------------------------------------------------------------------
# The report stays honest
# ---------------------------------------------------------------------------


class _Conf:
    scope_note = "scope"


class TestNotCovered:
    RUNS = {"observed_pct": 1.0, "asserted_pct": 0.0, "total_runs": 5}

    def _sys(self, tier=None):
        return ResourceGroup(
            id="s",
            tenant_id="t1",
            name="s",
            hierarchy_level=0,
            group_type=GroupType.AI_SYSTEM,
            eu_ai_act_risk_tier=tier,
        )

    def _ev(self, kind="impact_assessment", independent=None, expired=False):
        row = AISystemEvidence(
            id="ev-x",
            tenant_id="t1",
            ai_system_id="s",
            kind=kind,
            title="doc",
            independent=independent,
        )
        if expired:
            row.valid_until = datetime.now(timezone.utc) - timedelta(days=1)
        return row

    def test_no_evidence_at_all_is_named(self, h):
        gaps = _not_covered(self.RUNS, None, _Conf(), self._sys(), [])
        assert any("No external evidence" in g for g in gaps)

    def test_self_produced_only_is_named(self, h):
        gaps = _not_covered(
            self.RUNS, None, _Conf(), self._sys(), [self._ev(independent=False)]
        )
        assert any("independently produced" in g for g in gaps)

    def test_independent_evidence_clears_both_lines(self, h):
        gaps = _not_covered(
            self.RUNS, None, _Conf(), self._sys(), [self._ev(independent=True)]
        )
        assert not any("No external evidence" in g for g in gaps)
        assert not any("independently produced" in g for g in gaps)

    def test_high_risk_with_only_an_expired_assessment_is_named(self, h):
        gaps = _not_covered(
            self.RUNS,
            None,
            _Conf(),
            self._sys(tier="high"),
            [self._ev(independent=True, expired=True)],
        )
        assert any("no unexpired impact assessment" in g for g in gaps)

    def test_high_risk_with_a_live_assessment_is_not_named(self, h):
        gaps = _not_covered(
            self.RUNS,
            None,
            _Conf(),
            self._sys(tier="high"),
            [self._ev(independent=True)],
        )
        assert not any("impact assessment" in g for g in gaps)
