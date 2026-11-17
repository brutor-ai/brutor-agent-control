# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a186_composite_compliance — migration drift suite

Fresh installs build every table from the ORM (`create_all` + stamp);
upgrades apply a186_composite_compliance. Two of the columns are READ OR WRITTEN BY THE RUST CORE
(`proxy_logs.a2a_task_state` is written on outbound A2A rows,
`resource_groups.enforce_accepted_callers` is read by accepted-caller
enforcement) — a column present in one deployment shape and not the other
fails in the core, not at deploy time.
"""

from __future__ import annotations



from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.resource_group import ResourceGroup


#: (table, column) → the model that must carry it.


def test_to_dicts_expose_the_new_fields():
    e = AISystemDependency(id="dep-1", ai_system_id="rg", role="external",
                           supplier_legal_entity="Acme GmbH", separability_record_id="rec-1")
    d = e.to_dict()
    assert d["supplier_legal_entity"] == "Acme GmbH"
    assert d["separability_record_id"] == "rec-1" and "separability_sealed_at" in d
    assert AISystemHealth(id="h", ai_system_id="rg", dependency_cap={"reason": "dependency"}).to_dict()[
        "dependency_cap"] == {"reason": "dependency"}
    ev = AISystemEvidence(id="ev", tenant_id="t", ai_system_id="rg", dependency_id="dep-1")
    ev.kind = "supplier_information"
    assert ev.to_dict()["dependency_id"] == "dep-1"
    rg = ResourceGroup(id="g", tenant_id="t", name="g", enforce_accepted_callers=True)
    assert rg.to_dict()["enforce_accepted_callers"] is True
