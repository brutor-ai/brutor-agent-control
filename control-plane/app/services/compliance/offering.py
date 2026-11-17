# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Which compliance frameworks the product offers

Brutor does not ship support for a compliance
framework that has not been checked against its published text. A catalog is
OFFERED only when it is verified (every obligation cites the published text,
`tests/test_compliance_catalog_provenance.py`) and its instrument is final.

Everything that evaluates, reports, documents or enables a framework reads
catalogs through this module, never `catalogs.CATALOGS` / `get_catalog`
directly (`tests/test_compliance_offering.py` pins that), so an unverified
catalog cannot reach a user by a side door:

* enabling it for a tenant, or adding it to an AI System's profile, is
  refused (409) with the reason;
* a tenant or profile that enabled it earlier keeps the stored setting, but it
  is treated as off: no readiness, reports, documentation or incidents;
* the registry still lists it, marked not offered, with the reason.
"""

from __future__ import annotations

from typing import Dict, Optional

from fastapi import HTTPException

from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import (
    INSTRUMENT_DRAFT,
    INSTRUMENT_FINAL,
    INSTRUMENT_LICENSED,
    Framework,
)


def is_offered(framework: Framework) -> bool:
    """Verified against a final published instrument."""
    return framework.verified_against_instrument and framework.instrument_status == INSTRUMENT_FINAL


def unavailable_reason(framework: Framework) -> Optional[str]:
    """Why a catalog is not offered, in words a customer can read; None if it is."""
    if is_offered(framework):
        return None
    if framework.instrument_status == INSTRUMENT_DRAFT:
        return (
            f"{framework.title} is not final law yet; support follows once the "
            "published text is final and checked."
        )
    if framework.instrument_status == INSTRUMENT_LICENSED:
        return (
            f"{framework.title}'s text is licensed; support follows once the "
            "catalog has been checked against the full standard."
        )
    return (
        f"{framework.title} has not been checked against its published text yet; "
        "it is not available until it has."
    )


def catalog(framework_id: str) -> Optional[Framework]:
    """The catalog when it is offered; None when unknown OR not offered."""
    fw = CATALOGS.get(framework_id)
    return fw if fw is not None and is_offered(fw) else None


def any_catalog(framework_id: str) -> Optional[Framework]:
    """The catalog whether or not it is offered — for listing and explaining
    only (the registry view). Never for evaluation, reports or documents."""
    return CATALOGS.get(framework_id)


def offered_catalogs() -> Dict[str, Framework]:
    return {k: fw for k, fw in CATALOGS.items() if is_offered(fw)}


def require(framework_id: str) -> Framework:
    """The offered catalog, or 404 (unknown) / 409 (known, not offered)."""
    fw = CATALOGS.get(framework_id)
    if fw is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown compliance framework {framework_id!r}. Available: {sorted(offered_catalogs())}",
        )
    reason = unavailable_reason(fw)
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)
    return fw


__all__ = [
    "any_catalog",
    "catalog",
    "is_offered",
    "offered_catalogs",
    "require",
    "unavailable_reason",
]
