# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Brazil — AI Bill PL 2338/2023 — A BILL, NOT LAW

PL 2338/2023 is a bill. It passed the Federal Senate in December 2024 and
was sent to the Chamber of Deputies (received 17 March 2025). As of
2026-10-16 it has not been enacted: the Chamber's official record
(camara.leg.br, proposição 2487262) shows it awaiting the rapporteur's
opinion ("Aguardando Parecer"), last movement 2 September 2026, and any
Chamber amendments would return it to the Senate before sanction. There is
no Lei to check against, so this catalog is `INSTRUMENT_DRAFT` and
unverified.

It is a placeholder so the framework switcher can show it with the verify
banner: one entry that records the operator's own determination, with an
applicability date that is explicitly a placeholder (`PLACEHOLDER_DATE`)
and never "in force".

Replaced by a real catalog once the text is enacted (published by the
Presidency at planalto.gov.br) and checked against it. Not legal advice.
"""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

from app.services.compliance.catalogs import common as c
from app.services.compliance.catalogs.base import (
    INSTRUMENT_DRAFT,
    KIND_REGULATION,
    CatalogVersion,
    Framework,
    Obligation,
)

FRAMEWORK_ID = "brazil"
CATALOG_VERSION = "2026-09"

#: Not an enactment date — a far-future placeholder so nothing reads as in force.
PLACEHOLDER_DATE = date(2099, 12, 31)

OBLIGATIONS = [
    Obligation(
        key="pl2338-placeholder",
        title="Preliminary assessment and risk classification (bill pending)",
        reference="PL 2338/2023 (Senate text, December 2024)",
        applies_from=PLACEHOLDER_DATE,
        summary=(
            f"{c.CROSSWALK_PREFIX} the bill's preliminary risk assessment, should it be enacted: "
            "the operator's own classification is recorded. PL 2338/2023 is a bill pending in the "
            "Chamber of Deputies, not law; the date is a placeholder. " + c.UNVERIFIED_NOTE
        ),
        needs_review=True,
        review_note="Placeholder until PL 2338/2023 is enacted — no duty applies today.",
        evidence=[
            c.declared(
                "declared.preliminary_assessment",
                "frameworks.brazil.preliminary_assessment_reference",
                "A preliminary risk assessment is declared.",
                "Recorded for readiness only; no enacted duty exists to evidence.",
            )
        ],
        limit="Placeholder for a bill that is not law.",
    ),
]

FRAMEWORK = Framework(
    id=FRAMEWORK_ID,
    title="Brazil AI Bill (PL 2338/2023)",
    jurisdiction="BR",
    reference="Bill PL 2338/2023 (placeholder until enacted)",
    kind=KIND_REGULATION,
    obligations=OBLIGATIONS,
    versions=(CatalogVersion(CATALOG_VERSION, effective_from=PLACEHOLDER_DATE, note="Placeholder — bill pending"),),
    deadline_rules=MappingProxyType({}),
    retention_floors=MappingProxyType({}),
    verified_against_instrument=False,
    instrument_status=INSTRUMENT_DRAFT,
)

__all__ = ["FRAMEWORK", "FRAMEWORK_ID", "PLACEHOLDER_DATE"]
