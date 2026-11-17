# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
An unverified compliance framework is not offered.

Brutor does not ship support for a framework whose catalog has not been
checked against the published text. These tests make one catalog unverified
and one verified in-test (so they do not depend on which catalogs have been
checked) and pin, over HTTP:

* the registry lists it as not offered, with the reason, and enabling it is
  refused (409); a tenant that enabled it earlier sees it off;
* evaluation for it is refused (409), not computed;
* an AI System profile cannot ADD it (409), but a profile that already has it
  can still be saved (so unrelated edits are not blocked);

and, as a class guard, that nothing in the app reads the catalogs except
through `compliance.offering`.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.endpoints.admin.compliance_evaluation import create_compliance_evaluation_router
from app.api.v1.endpoints.admin.compliance_registry import create_compliance_registry_router
from app.core.logging_system import BrutorLogger
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.database.seed_compliance_frameworks import seed_compliance_frameworks
from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import INSTRUMENT_DRAFT, INSTRUMENT_FINAL
from tests.admin_endpoint_harness import build_admin_harness
from tests.source_trees import python_sources

TENANT = "t1"
SYSTEM = "sys-offering"
UNVERIFIED = "hipaa"  # made unverified in-test
VERIFIED = "eu-ai-act"  # made verified in-test


def _seed(session):
    seed_compliance_frameworks(session)
    t = session.get(Tenant, TENANT)
    # Enabled earlier, before the framework stopped being offered.
    t.compliance_profile = {"frameworks": [UNVERIFIED]}
    session.add(ResourceGroup(
        id=SYSTEM, tenant_id=TENANT, name=SYSTEM, description=SYSTEM, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
        eu_ai_act_risk_tier="high", eu_ai_act_role="deployer",
    ))


@pytest.fixture(autouse=True)
def _seal(monkeypatch):
    monkeypatch.setattr(
        "app.services.evidence.core_client.seal",
        lambda tenant_id, records, **_kw: {"sealed": [{"record_id": "0" * 64, "log_id": "l", "seq": 1}]},
    )


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setitem(CATALOGS, UNVERIFIED, replace(CATALOGS[UNVERIFIED], verified_against_instrument=False))
    monkeypatch.setitem(
        CATALOGS, VERIFIED,
        replace(CATALOGS[VERIFIED], verified_against_instrument=True, instrument_status=INSTRUMENT_FINAL),
    )


@pytest.fixture(scope="module")
def harness():
    log = BrutorLogger("offering-tests")
    h = build_admin_harness([create_compliance_registry_router(log), create_compliance_evaluation_router(log)])
    h.seed(_seed)
    yield h


def _frameworks(harness):
    r = harness.client.get("/v1/admin/compliance/frameworks", headers=harness.headers())
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body if isinstance(body, list) else body.get("frameworks") or body.get("items")
    return {row["id"]: row for row in rows}


def test_the_registry_says_which_frameworks_are_offered_and_why(harness):
    rows = _frameworks(harness)
    assert rows[VERIFIED]["offered"] is True and rows[VERIFIED]["unavailable_reason"] is None
    assert rows[UNVERIFIED]["offered"] is False
    assert "not been checked against its published text" in rows[UNVERIFIED]["unavailable_reason"]


def test_a_framework_enabled_before_it_was_withdrawn_reads_as_off(harness):
    assert _frameworks(harness)[UNVERIFIED]["enabled"] is False


def test_enabling_an_unverified_framework_is_refused(harness):
    r = harness.client.patch(
        f"/v1/admin/compliance/frameworks/{UNVERIFIED}", json={"enabled": True}, headers=harness.headers()
    )
    assert r.status_code == 409, r.text
    # Turning it off is always allowed.
    r = harness.client.patch(
        f"/v1/admin/compliance/frameworks/{UNVERIFIED}", json={"enabled": False}, headers=harness.headers()
    )
    assert r.status_code == 200, r.text


def test_evaluation_for_an_unverified_framework_is_refused(harness):
    r = harness.client.get(
        "/v1/admin/compliance/overview", params={"framework": UNVERIFIED}, headers=harness.headers()
    )
    assert r.status_code == 409, r.text
    r = harness.client.get(
        "/v1/admin/compliance/overview", params={"framework": VERIFIED}, headers=harness.headers()
    )
    assert r.status_code == 200, r.text


def test_a_draft_instrument_says_it_is_not_law_yet(monkeypatch, harness):
    monkeypatch.setitem(CATALOGS, "brazil", replace(CATALOGS["brazil"], instrument_status=INSTRUMENT_DRAFT,
                                                     verified_against_instrument=False))
    assert "not final law yet" in _frameworks(harness)["brazil"]["unavailable_reason"]


def test_a_profile_cannot_add_an_unverified_framework_but_keeps_one_it_has(harness):
    path = f"/v1/admin/compliance/ai-systems/{SYSTEM}/profile"
    base = {"core": {"operator_role": "builder"},
            "frameworks": {VERIFIED: {"role": "deployer", "risk_tier": "high"}}}
    r = harness.client.put(path, json=base, headers=harness.headers())
    assert r.status_code == 200, r.text

    adding = {**base, "frameworks": {**base["frameworks"],
                                     UNVERIFIED: {"covered_entity": False, "phi_in_scope": False}}}
    r = harness.client.put(path, json=adding, headers=harness.headers())
    assert r.status_code == 409, r.text

    # A profile saved while the framework was offered keeps it on later saves.
    from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
    s = harness.Factory()
    try:
        row = s.get(AISystemComplianceProfile, SYSTEM)
        row.frameworks = {**(row.frameworks or {}), UNVERIFIED: {"covered_entity": False, "phi_in_scope": False}}
        s.commit()
    finally:
        s.close()
    r = harness.client.put(path, json=adding, headers=harness.headers())
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Class guard: one way in
# ---------------------------------------------------------------------------

APP = Path(__file__).resolve().parents[1] / "app"
ALLOWED = {
    APP / "services" / "compliance" / "offering.py",
}
#: Reading the compliance catalogs directly: importing the registry (or its
#: lookup) from the catalogs package, or the old aliases for it.
DIRECT = re.compile(
    r"from\s+app\.services\.compliance\.catalogs\s+import\s[^\n]*\b(CATALOGS|get_catalog)\b"
    r"|\bOBLIGATION_CATALOGS\b|\bai_obligations\s+import\s[^\n]*\bFRAMEWORKS\b|\bFRAMEWORKS\s*[.\[]"
)


def _violations(path: Path, text: str):
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    return [f"{path}: {m.group(0)}" for m in DIRECT.finditer(code)]


def test_catalogs_are_read_only_through_offering():
    bad = []
    for path in python_sources(APP):
        if path in ALLOWED or (APP / "services" / "compliance" / "catalogs") in path.parents:
            continue
        bad += _violations(path, path.read_text())
    assert not bad, "read catalogs through app.services.compliance.offering:\n" + "\n".join(bad)


def test_the_guard_catches_a_direct_read():
    assert _violations(Path("x.py"), "from app.services.compliance.catalogs import CATALOGS\n")
    assert _violations(Path("x.py"), "from app.services.compliance.catalogs import get_catalog\nget_catalog(f)\n")
    assert _violations(Path("x.py"), "fw = FRAMEWORKS.get(fid)\n")
    assert not _violations(Path("x.py"), "# from app.services.compliance.catalogs import CATALOGS\n")
    # Another subsystem's catalog is not the compliance registry.
    assert not _violations(Path("x.py"), "entries = get_catalog()\n")
