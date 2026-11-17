# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# NIST AI Risk Management Framework 1.0 (`voluntary`)

NIST AI 100-1 (January 2023). Voluntary: there is nothing to comply with,
only practices an organisation can show it follows. Entries use the
crosswalk's language — the gateway's records "can serve as evidence
toward" a subcategory; they never satisfy one.

Checked on 2026-10-16 against the published text of NIST AI 100-1, AI RMF
1.0 (January 2023), as served at
nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf (Tables 1, 3 and 4). Every
subcategory id and its wording below was compared with that text and each
obligation cites a verbatim excerpt. AI RMF 1.0 is still the current
version on that date: NIST's AI RMF page states that "The AI RMF 1.0 is
being revised as part of the White House AI Action Plan", but no 1.1 or
successor has been published. The companion Generative AI Profile (NIST AI
600-1, July 2024) and the critical-infrastructure profile concept note
(April 2026) are not catalogued here. The framework is voluntary
(AI 100-1 Executive Summary: "The Framework is intended to be voluntary"), so no entry describes anything
as legally required. Not legal advice.
"""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    INSTRUMENT_FINAL,
    KIND_VOLUNTARY,
    CatalogVersion,
    Framework,
    Obligation,
    Source,
)

FRAMEWORK_ID = "nist-ai-rmf"
CATALOG_VERSION = "2026-09"

#: AI RMF 1.0 release.
RELEASED = date(2023, 1, 26)

#: When every entry was checked against the published text.
CHECKED = date(2026, 10, 16)

_INSTRUMENT = "NIST AI 100-1, Artificial Intelligence Risk Management Framework (AI RMF 1.0), January 2023"
_URL = "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf"

_E = c.CROSSWALK_PREFIX


def _src(provision: str, page: int, excerpt: str) -> Source:
    return Source(
        instrument=_INSTRUMENT,
        provision=provision,
        url=f"{_URL}#page={page}",
        checked_on=CHECKED,
        excerpt=excerpt,
    )


_O = Obligation

OBLIGATIONS = [
    _O(
        key="govern-1-1",
        title="GOVERN 1.1 — legal and regulatory requirements",
        reference="GOVERN 1.1",
        applies_from=RELEASED,
        summary=(
            f"{_E} understanding and managing legal and regulatory requirements involving AI: "
            "a sealed, witnessed record of every governed action."
        ),
        evidence=[c.every_verdict_sealed(), c.witnessed()],
        sources=(_src("GOVERN 1.1 (Table 1)", 27, "GOVERN 1.1: Legal and regulatory requirements involving AI are understood, managed, and documented."),),
    ),
    _O(
        key="govern-6-1",
        title="GOVERN 6.1 — third-party risk policies",
        reference="GOVERN 6.1",
        applies_from=RELEASED,
        summary=(
            f"{_E} policies and procedures addressing AI risks associated with third-party "
            "entities: the operator role and "
            "vendor-operated systems are declared."
        ),
        evidence=[
            c.declared(
                "declared.operator_role",
                "core.operator_role",
                "The organisation's role for this system (builder, operator, vendor) is declared.",
                "Third-party policies are organisational; they are not evaluated.",
            ),
            c.least_privilege_declared(),
        ],
        sources=(_src("GOVERN 6.1 (Table 1)", 29, "GOVERN 6.1: Policies and procedures are in place that address AI risks associated with third-party entities, including risks of infringement of a third-party’s intellectual property or other rights."),),
    ),
    _O(
        key="measure-2-6",
        title="MEASURE 2.6 — safety evaluated regularly",
        reference="MEASURE 2.6",
        applies_from=RELEASED,
        summary=(
            f"{_E} regular safety evaluation and the ability to fail safely: daily health and "
            "the stop controls."
        ),
        evidence=[c.daily_health(), c.suspend_available()],
        sources=(_src("MEASURE 2.6 (Table 3)", 35, "MEASURE 2.6: The AI system is evaluated regularly for safety risks – as identified in the MAP function. The AI system to be deployed is demonstrated to be safe, its residual negative risk does not exceed the risk tolerance, and it can fail safely"),),
    ),
    _O(
        key="measure-2-8",
        title="MEASURE 2.8 — transparency and accountability",
        reference="MEASURE 2.8",
        applies_from=RELEASED,
        summary=(
            f"{_E} examining transparency and accountability risks: closed record chains and an "
            "intact audit trail."
        ),
        evidence=[c.chain_complete(), c.integrity_checked()],
        sources=(_src("MEASURE 2.8 (Table 3)", 35, "MEASURE 2.8: Risks associated with transparency and accountability – as identified in the MAP function – are examined and documented."),),
    ),
    _O(
        key="manage-1-3",
        title="MANAGE 1.3 — responses to high risks",
        reference="MANAGE 1.3",
        applies_from=RELEASED,
        summary=(
            f"{_E} planned and documented responses to high-priority risks: human override "
            "and the record of its use."
        ),
        evidence=[c.override_available(), c.oversight_exercised()],
        sources=(_src("MANAGE 1.3 (Table 4)", 37, "MANAGE 1.3: Responses to the AI risks deemed high priority, as identified by the MAP function, are developed, planned, and documented. Risk response options can include mitigating, transferring, avoiding, or accepting."),),
    ),
    _O(
        key="manage-4-1",
        title="MANAGE 4.1 — post-deployment monitoring",
        reference="MANAGE 4.1",
        applies_from=RELEASED,
        summary=(
            f"{_E} post-deployment monitoring plans, including incident response: daily "
            "monitoring and the incident register."
        ),
        evidence=[c.daily_health(), c.incident_register()],
        sources=(_src("MANAGE 4.1 (Table 4)", 38, "MANAGE 4.1: Post-deployment AI system monitoring plans are implemented, including mechanisms for capturing and evaluating input from users and other relevant AI actors, appeal and override, decommissioning, incident response, recovery, and change management."),),
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="NIST AI RMF 1.0",
    jurisdiction="US",
    reference="NIST AI 100-1, Artificial Intelligence Risk Management Framework (AI RMF 1.0)",
    kind=KIND_VOLUNTARY,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=RELEASED, note="AI RMF 1.0 (January 2023)"),),
    deadline_rules=MappingProxyType({}),
    retention_floors=MappingProxyType({}),
    verified_against_instrument=True,
    instrument_status=INSTRUMENT_FINAL,
    verified_on=CHECKED,
)

__all__ = ["FRAMEWORK", "FRAMEWORK_ID"]
