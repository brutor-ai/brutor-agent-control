# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The compliance framework registry — data, not code

A framework is a row: its title, kind, jurisdiction, the instrument it
cites, the catalog version, its retention floor and deadline rules, and
whether the catalog was verified against the published instrument or
written from memory. The five tagging frameworks carry their
`tag_key`, so the Python and Rust tagging engines and this registry read
one set of ids; DORA, NIS2 and NIST AI RMF have no tagging yet and are
enabled per tenant through `compliance_profile.registry_enabled` instead.

Tenant enablement is NOT a column here: the registry is platform-wide
reference data, and which frameworks a tenant has declared lives on the
tenant (`tenants.compliance_profile`). `app/services/compliance_registry.py`
joins the two.

The rows are written by `app/database/seed_compliance_frameworks.py` on
every start, from `FRAMEWORK_SEED` below; no revision writes them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

KIND_REGULATION = "regulation"
KIND_STANDARD = "standard"
KIND_ATTESTATION = "attestation"
KIND_VOLUNTARY = "voluntary"
FRAMEWORK_KINDS = frozenset({KIND_REGULATION, KIND_STANDARD, KIND_ATTESTATION, KIND_VOLUNTARY})

CATALOG_VERSION = "2026-09"

#: The registry seed. Order = `sort_order`.
FRAMEWORK_SEED: List[Dict[str, Any]] = [
    {
        "id": "eu-ai-act",
        "tag_key": "eu_ai_act",
        "title": "EU AI Act",
        "kind": KIND_REGULATION,
        "jurisdiction": "EU",
        "reference": "Regulation (EU) 2024/1689, as amended by Regulation (EU) 2026/1744",
        "verified_against_instrument": True,
        "retention_floor_days": 183,
        "deadline_rules": {
            "serious_incident": {"standard_days": 15, "death_days": 10, "widespread_days": 2}
        },
        "sort_order": 10,
    },
    {
        "id": "iso-42001",
        "tag_key": "iso_42001",
        "title": "ISO/IEC 42001:2023",
        "kind": KIND_STANDARD,
        "jurisdiction": "Global",
        "reference": "ISO/IEC 42001:2023 Artificial intelligence management system",
        "verified_against_instrument": True,
        "retention_floor_days": None,
        "deadline_rules": {},
        "sort_order": 20,
    },
    {
        "id": "soc2",
        "tag_key": "soc2",
        "title": "SOC 2 (AICPA Trust Services Criteria)",
        "kind": KIND_ATTESTATION,
        "jurisdiction": "US",
        "reference": "AICPA TSC 2017 (2022 revision)",
        "verified_against_instrument": True,
        "retention_floor_days": 365,
        "deadline_rules": {},
        "sort_order": 30,
    },
    {
        "id": "hipaa",
        "tag_key": "hipaa",
        "title": "HIPAA Security Rule",
        "kind": KIND_REGULATION,
        "jurisdiction": "US",
        "reference": "45 CFR Part 164 Subpart C; Breach Notification Rule §164.404",
        "verified_against_instrument": True,
        "retention_floor_days": 2190,
        "deadline_rules": {"breach_notification": {"standard_days": 60}},
        "sort_order": 40,
    },
    {
        "id": "gdpr",
        "tag_key": "gdpr_art_30",
        "title": "GDPR",
        "kind": KIND_REGULATION,
        "jurisdiction": "EU",
        "reference": "Regulation (EU) 2016/679",
        "verified_against_instrument": True,
        "retention_floor_days": None,
        "deadline_rules": {"breach_notification": {"standard_hours": 72}},
        "sort_order": 50,
    },
    {
        "id": "dora",
        "tag_key": None,
        "title": "DORA",
        "kind": KIND_REGULATION,
        "jurisdiction": "EU",
        "reference": "Regulation (EU) 2022/2554",
        "verified_against_instrument": False,
        "retention_floor_days": None,
        "deadline_rules": {
            "major_incident": {"initial_hours": 4, "intermediate_hours": 72, "final_days": 30}
        },
        "sort_order": 60,
    },
    {
        "id": "nis2",
        "tag_key": None,
        "title": "NIS2",
        "kind": KIND_REGULATION,
        "jurisdiction": "EU",
        "reference": "Directive (EU) 2022/2555",
        "verified_against_instrument": False,
        "retention_floor_days": None,
        "deadline_rules": {
            "significant_incident": {
                "early_warning_hours": 24,
                "notification_hours": 72,
                "final_days": 30,
            }
        },
        "sort_order": 70,
    },
    {
        "id": "nist-ai-rmf",
        "tag_key": None,
        "title": "NIST AI RMF 1.0",
        "kind": KIND_VOLUNTARY,
        "jurisdiction": "US",
        "reference": "NIST AI 100-1 (January 2023)",
        "verified_against_instrument": False,
        "retention_floor_days": None,
        "deadline_rules": {},
        "sort_order": 80,
    },
    # Written from the instruments' public summaries, not verified
    # against the enacted text; the console shows a verify banner.
    {
        "id": "colorado",
        "tag_key": None,
        "title": "Colorado AI Act (SB 24-205)",
        "kind": KIND_REGULATION,
        "jurisdiction": "US-CO",
        "reference": "Colorado SB 24-205, Consumer Protections for Artificial Intelligence (as amended)",
        "verified_against_instrument": False,
        "retention_floor_days": None,
        # SB 26-189 repealed and reenacted the Act: no AG notice deadline.
        "deadline_rules": {},
        "sort_order": 90,
    },
    {
        "id": "korea",
        "tag_key": None,
        "title": "Korea AI Framework Act",
        "kind": KIND_REGULATION,
        "jurisdiction": "KR",
        "reference": "Framework Act on the Development of Artificial Intelligence and Establishment of Trust",
        "verified_against_instrument": False,
        "retention_floor_days": None,
        "deadline_rules": {},
        "sort_order": 100,
    },
    {
        "id": "brazil",
        "tag_key": None,
        "title": "Brazil AI Bill (PL 2338/2023)",
        "kind": KIND_REGULATION,
        "jurisdiction": "BR",
        "reference": "Bill PL 2338/2023 (placeholder until enacted)",
        "verified_against_instrument": False,
        "retention_floor_days": None,
        "deadline_rules": {},
        "sort_order": 110,
    },
]

def registry_tag_keys() -> Dict[str, str]:
    """registry id → tagging key, for every framework that has tagging.
    The Python tagging engine and the admin compliance
    models read their framework keys from here; a drift test pins the Rust
    core's `FRAMEWORK_*` constants against the same set."""
    return {row["id"]: row["tag_key"] for row in FRAMEWORK_SEED if row["tag_key"]}


def tag_key_for(framework_id: str) -> str:
    try:
        return registry_tag_keys()[framework_id]
    except KeyError as exc:
        raise KeyError(f"framework {framework_id!r} has no tagging key in the registry") from exc


#: Fields the seed owns and keeps current on every boot.
SEED_FIELDS = (
    "tag_key",
    "title",
    "kind",
    "jurisdiction",
    "reference",
    "verified_against_instrument",
    "retention_floor_days",
    "deadline_rules",
    "sort_order",
)


class ComplianceFramework(Base):
    """One registry row. `id` is the framework slug (`eu-ai-act`)."""

    __tablename__ = "compliance_frameworks"

    id = Column(String(64), primary_key=True, comment="Slug: eu-ai-act, iso-42001, soc2 …")
    tag_key = Column(
        String(64),
        nullable=True,
        comment="The tagging key (eu_ai_act, gdpr_art_30 …); NULL when the framework has no tagging",
    )
    title = Column(String(255), nullable=False)
    kind = Column(String(16), nullable=False, comment="regulation | standard | attestation | voluntary")
    jurisdiction = Column(String(64), nullable=False)
    reference = Column(Text, nullable=False, comment="The instrument cited")
    catalog_version = Column(
        String(32), nullable=False, default=CATALOG_VERSION, server_default=CATALOG_VERSION
    )
    verified_against_instrument = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="False = written from memory; the UI shows a verify-before-relying banner",
    )
    retention_floor_days = Column(Integer, nullable=True)
    deadline_rules = Column(JSONB, nullable=False, default=dict, server_default="{}")
    sort_order = Column(Integer, nullable=False, default=100, server_default="100")

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    @validates("kind")
    def _validate_kind(self, _key, value):
        if value not in FRAMEWORK_KINDS:
            raise ValueError(f"kind must be one of {sorted(FRAMEWORK_KINDS)}, got {value!r}")
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tag_key": self.tag_key,
            "title": self.title,
            "kind": self.kind,
            "jurisdiction": self.jurisdiction,
            "reference": self.reference,
            "catalog_version": self.catalog_version,
            "verified_against_instrument": bool(self.verified_against_instrument),
            "retention_floor_days": self.retention_floor_days,
            "deadline_rules": self.deadline_rules or {},
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "ComplianceFramework",
    "FRAMEWORK_SEED",
    "registry_tag_keys",
    "tag_key_for",
    "SEED_FIELDS",
    "FRAMEWORK_KINDS",
    "CATALOG_VERSION",
    "KIND_REGULATION",
    "KIND_STANDARD",
    "KIND_ATTESTATION",
    "KIND_VOLUNTARY",
]
