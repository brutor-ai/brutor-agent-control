# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Provider Catalog admin API (browse-only)

Endpoint under `/v1/admin/provider-catalog`:

  GET ""  — browse / search the global provider catalog (filters +
            pagination)

The catalog is GLOBAL (no tenant scope). This is what the admin UI
provider dropdown reads so an operator can pick ANY provider when
manually adding a model — the actual `llm_providers` row is materialized
on demand (`upsert_provider_for_key`) when the model is created.

No sysadmin CRUD is exposed here: builtin entries are seeded from
`PROVIDER_SEED_CATALOG` (mirroring how the model catalog is seeded), and
the materialized per-tenant `llm_providers` rows remain editable via the
existing `/v1/admin/providers/*` router.

Read route requires `AI_MODEL_READ` (mirrors `/v1/admin/llm-catalog`).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.v1.models.admin.llm_provider_catalog import (
    LlmProviderCatalogEntryResponse,
    LlmProviderCatalogListResponse,
)
from app.core.admin_user.dependencies import (
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.llm_provider_catalog import LlmProviderCatalog


def create_llm_provider_catalog_router(logger: BrutorLogger) -> APIRouter:
    """Build the `/admin/provider-catalog` router."""

    router = APIRouter(
        prefix="/admin/provider-catalog", tags=["LLM Provider Catalog"]
    )

    @router.get(
        "",
        response_model=LlmProviderCatalogListResponse,
        summary="Browse the global LLM provider catalog",
    )
    def list_provider_catalog(
        category: Optional[str] = Query(
            None, description="Filter by category (chat, image, audio, ...)"
        ),
        q: Optional[str] = Query(
            None, description="Substring on display_name / key"
        ),
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_MODEL_READ)),
    ) -> LlmProviderCatalogListResponse:
        where = []
        if category is not None:
            where.append(LlmProviderCatalog.category == category)
        if q:
            needle = f"%{q.lower()}%"
            where.append(
                func.lower(LlmProviderCatalog.display_name).like(needle)
                | func.lower(LlmProviderCatalog.key).like(needle)
            )
        clause = and_(*where) if where else None

        total_stmt = select(func.count()).select_from(LlmProviderCatalog)
        if clause is not None:
            total_stmt = total_stmt.where(clause)
        total = db.execute(total_stmt).scalar_one()

        rows_stmt = select(LlmProviderCatalog)
        if clause is not None:
            rows_stmt = rows_stmt.where(clause)
        rows_stmt = (
            rows_stmt.order_by(LlmProviderCatalog.display_name.asc())
            .limit(limit)
            .offset(offset)
        )
        rows = db.execute(rows_stmt).scalars().all()

        return LlmProviderCatalogListResponse(
            providers=[
                LlmProviderCatalogEntryResponse(**r.to_dict()) for r in rows
            ],
            total=int(total or 0),
        )

    return router
