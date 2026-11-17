# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Artifact standards — the schemas themselves

    GET /v1/admin/standards                                    — what exists
    GET /v1/admin/standards/ai-system-contract/schema          — JSON Schema
    GET /v1/admin/standards/ai-system-assurance-report/schema  — JSON Schema

A document format that claims to be a standard has to publish its schema:
"valid" must be checkable by a party that has never seen Brutor. These
endpoints serve the exact vendored schemas the exporters are pinned to,
so the schema a recipient downloads is the one the artifact was built
against — not a docs page that drifted.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.core.admin_user.dependencies import get_current_admin
from app.core.logging_system import BrutorLogger

from app import standards

_SLUGS = {
    "ai-system-contract": standards.CONTRACT_SPEC,
    "ai-system-assurance-report": standards.REPORT_SPEC,
}


def create_standards_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/standards", tags=["Standards"])

    @router.get("", summary="The artifact standards this deployment speaks")
    async def list_standards(
        _admin=Depends(get_current_admin),
    ) -> Dict[str, Any]:
        return {
            "standards": [
                {
                    "slug": slug,
                    "spec": spec,
                    "spec_version": (
                        standards.CONTRACT_SPEC_VERSION
                        if spec == standards.CONTRACT_SPEC
                        else standards.REPORT_SPEC_VERSION
                    ),
                    "schema_path": f"/v1/admin/standards/{slug}/schema",
                }
                for slug, spec in _SLUGS.items()
            ]
        }

    @router.get("/{slug}/schema", summary="The JSON Schema for one standard")
    async def get_schema(
        slug: str,
        _admin=Depends(get_current_admin),
    ) -> Dict[str, Any]:
        spec = _SLUGS.get(slug)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown standard {slug!r}; known: {sorted(_SLUGS)}",
            )
        return standards.load_schema(spec)

    return router
