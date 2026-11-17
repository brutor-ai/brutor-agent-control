# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The two assurance artifacts, as would-be standards

Like policy-as-code, the AI System Contract and the AI System Assurance
Report are meant to travel: out of this deployment, into a GRC tool, a
procurement review, another vendor's verifier. That only works if the
artifact is a **specified document**, not "whatever our API happened to
return that week". This package is the specification surface:

* a stable spec identifier + semantic version on every exported document;
* a vendored JSON Schema per artifact, served over the API and pinned by
  contract tests, so "valid" is checkable by a party that has never seen
  Brutor;
* canonical builders, so the exported bytes come from exactly one place.

## Versioning discipline

`1.x` may add optional fields; anything that removes or re-types a field
is `2.0`. The schema is the arbiter — if a change would make a previously
valid document invalid, the major version moves. Consumers should accept
unknown fields (the schemas set `additionalProperties: true` at the
document level for exactly this reason).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

CONTRACT_SPEC = "ai.brutor/ai-system-contract"
CONTRACT_SPEC_VERSION = "1.0"

REPORT_SPEC = "ai.brutor/ai-system-assurance-report"
REPORT_SPEC_VERSION = "1.10"  # 1.1 baselines/recommended_controls; 1.2 assurance_summary/verdict.basis/drift values; 1.3 status vocab + exec rows + level.scale; 1.4 why_it_matters; 1.5 obligation_evidence; 1.6 composition; 1.7 composition.agents + agent_findings; 1.8 judgement_exclusions + coverage.excluded_period; 1.9 judgement_exclusions.measurement_changes + measurement_change epochs; 1.10 judgement_exclusions.excluded_approval_requests + coverage.excluded_period.excluded_approval_requests (all additive)

_HERE = Path(__file__).parent

_SCHEMA_FILES = {
    CONTRACT_SPEC: "ai_system_contract.schema.json",
    REPORT_SPEC: "ai_system_assurance_report.schema.json",
}


def load_schema(spec: str) -> Dict[str, Any]:
    """The vendored JSON Schema for a spec. KeyError for unknown specs."""
    return json.loads((_HERE / _SCHEMA_FILES[spec]).read_text())


def contract_document(contract, system) -> Dict[str, Any]:
    """The canonical AI System Contract document.

    Self-contained on purpose: the closure, the hash over it, who approved
    it and when — everything a third party needs to check that the closure
    hashes to what the approval names, without an API call back to us.
    """
    return {
        "spec": CONTRACT_SPEC,
        "spec_version": CONTRACT_SPEC_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": contract.tenant_id,
        "system": {
            "id": system.id,
            "name": system.display_name or system.name,
            "system_kind": system.system_kind,
        },
        "contract": {
            "version": contract.version,
            "hash": contract.contract_hash,
            "hash_algorithm": "sha256(canonical_json(closure))",
            "status": contract.status,
            "created_at": (
                contract.created_at.isoformat() if contract.created_at else None
            ),
            "created_by": contract.created_by,
            "approved_by": contract.approved_by,
            "approved_at": (
                contract.approved_at.isoformat() if contract.approved_at else None
            ),
            "promoted_at": (
                contract.promoted_at.isoformat() if contract.promoted_at else None
            ),
            "diff_from_version": contract.diff_from_version,
            "diff_summary": contract.diff_summary,
        },
        "closure": contract.resolved_closure,
    }


__all__ = [
    "CONTRACT_SPEC",
    "CONTRACT_SPEC_VERSION",
    "REPORT_SPEC",
    "REPORT_SPEC_VERSION",
    "contract_document",
    "load_schema",
]
