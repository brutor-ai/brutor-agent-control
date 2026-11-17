# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Skill Sources admin endpoints — git-sourced Agent Skill repos.

# Operator-facing endpoints (RBAC-gated under AGENT_SKILL_*):
    GET    /v1/admin/skill-sources
    GET    /v1/admin/skill-sources/{id}
    POST   /v1/admin/skill-sources
    PATCH  /v1/admin/skill-sources/{id}
    DELETE /v1/admin/skill-sources/{id}
    POST   /v1/admin/skill-sources/{id}/refresh     (manual sync trigger)

# Scheduler-facing endpoints (called by gateway-core's skill_source_sync
# scheduler — same trust boundary as the rest of the proxy → control
# channel; RBAC-gated under AGENT_SKILL_UPDATE which only the system
# admin path holds):
    POST   /v1/admin/skill-sources/{id}/sync-ingest  (here are the discoveries)
    POST   /v1/admin/skill-sources/{id}/sync-error   (sync failed)

# Why we keep the same Permission tokens

Adding `SKILL_SOURCE_*` permissions would over-segment the admin RBAC
surface. A source IS an admin-tier configuration of how Agent Skills get
into the catalog — the same admin who can author skills can register
sources.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from ulid import ULID

from app.constants.admin import AdminType
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.services import agent_skill_size
from app.core.admin_user.dependencies import (
    get_audit_logger,
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.agent_skill import AgentSkill
from app.database.models.skill_source import SkillSource, SYNC_STATUS_IDLE
from app.api.v1.models.admin.skill_source import (
    DiscoveredSkill,
    SkillSourceCreate,
    SkillSourceIngestRequest,
    SkillSourceIngestSummary,
    SkillSourceListResponse,
    SkillSourceResponse,
    SkillSourceSyncErrorRequest,
    SkillSourceUpdate,
)
from app.constants.agent_skill import SkillStatus


def _skill_source_to_response(source: SkillSource) -> SkillSourceResponse:
    return SkillSourceResponse(**source.to_dict())


def create_skill_sources_admin_router(logger: BrutorLogger) -> APIRouter:
    """Factory mirrors `create_agent_skills_admin_router` — see that
    file's docstring for the convention.
    """

    router = APIRouter(prefix="/admin/skill-sources", tags=["Skill Sources Admin"])

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    @router.get(
        "",
        response_model=SkillSourceListResponse,
        summary="List registered Skill Sources",
    )
    def list_sources(
        request: Request,
        enabled: Optional[bool] = Query(None),
        status_filter: Optional[str] = Query(None, alias="status"),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_READ)
        ),
    ) -> SkillSourceListResponse:
        _, tenant_id, _ = admin_and_tenant
        q = db.query(SkillSource).filter(SkillSource.tenant_id == tenant_id)
        if enabled is not None:
            q = q.filter(SkillSource.enabled == enabled)
        if status_filter:
            q = q.filter(SkillSource.status == status_filter)
        rows = q.order_by(SkillSource.created_at.desc()).all()
        return SkillSourceListResponse(
            sources=[_skill_source_to_response(r) for r in rows],
            total=len(rows),
        )

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    @router.get(
        "/{source_id}",
        response_model=SkillSourceResponse,
        summary="Get a Skill Source",
    )
    def get_source(
        source_id: str,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_READ)
        ),
    ) -> SkillSourceResponse:
        _, tenant_id, _ = admin_and_tenant
        row = (
            db.query(SkillSource)
            .filter(
                and_(SkillSource.id == source_id, SkillSource.tenant_id == tenant_id)
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="skill source not found")
        return _skill_source_to_response(row)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @router.post(
        "",
        response_model=SkillSourceResponse,
        status_code=201,
        summary="Register a Skill Source",
    )
    def create_source(
        payload: SkillSourceCreate,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_CREATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> SkillSourceResponse:
        admin_row, tenant_id, _ = admin_and_tenant

        # Pre-check uniqueness — the DB also enforces this via unique
        # constraint but we want a 409 not a 500.
        existing = (
            db.query(SkillSource)
            .filter(
                and_(
                    SkillSource.tenant_id == tenant_id,
                    SkillSource.repo_url == payload.repo_url,
                    SkillSource.git_ref == payload.git_ref,
                )
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a skill source for {payload.repo_url}@{payload.git_ref} "
                    f"already exists (id={existing.id})"
                ),
            )

        row = SkillSource(
            id=f"sks-{ULID()}",
            tenant_id=tenant_id,
            name=payload.name,
            repo_url=payload.repo_url,
            git_ref=payload.git_ref,
            subpath=payload.subpath,
            enabled=payload.enabled,
            sync_interval_seconds=payload.sync_interval_seconds,
            auto_publish=payload.auto_publish,
            github_token_secret_ref=payload.github_token_secret_ref,
            created_by=admin_row.id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        audit.log(
            action="skill_source_create",
            db=db,
            admin=admin_row,
            tenant_id=tenant_id,
            resource_type="skill_source",
            resource_id=row.id,
            details={
                "name": row.name,
                "repo_url": row.repo_url,
                "git_ref": row.git_ref,
            },
        )
        logger.info(
            "skill_source created",
            context={"id": row.id, "tenant_id": tenant_id, "repo": row.repo_url},
        )
        return _skill_source_to_response(row)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    @router.patch(
        "/{source_id}",
        response_model=SkillSourceResponse,
        summary="Update a Skill Source (mutable subset)",
    )
    def update_source(
        source_id: str,
        payload: SkillSourceUpdate,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> SkillSourceResponse:
        admin_row, tenant_id, _ = admin_and_tenant
        row = (
            db.query(SkillSource)
            .filter(
                and_(SkillSource.id == source_id, SkillSource.tenant_id == tenant_id)
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="skill source not found")

        changed: dict = {}
        for field in (
            "name",
            "subpath",
            "enabled",
            "sync_interval_seconds",
            "auto_publish",
            "github_token_secret_ref",
        ):
            new_val = getattr(payload, field, None)
            if new_val is not None and getattr(row, field) != new_val:
                changed[field] = {"old": getattr(row, field), "new": new_val}
                setattr(row, field, new_val)

        if changed:
            db.commit()
            db.refresh(row)
            audit.log(
                action="skill_source_update",
                db=db,
                admin=admin_row,
                tenant_id=tenant_id,
                resource_type="skill_source",
                resource_id=row.id,
                details=changed,
            )

        return _skill_source_to_response(row)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @router.delete(
        "/{source_id}",
        status_code=204,
        summary="Delete a Skill Source (orphans its skills as Manual)",
    )
    def delete_source(
        source_id: str,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_DELETE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_row, tenant_id, _ = admin_and_tenant
        row = (
            db.query(SkillSource)
            .filter(
                and_(SkillSource.id == source_id, SkillSource.tenant_id == tenant_id)
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="skill source not found")

        # The ON DELETE SET NULL on agent_skills.source_repo_id takes care
        # of orphaning. We deliberately do NOT cascade-delete skills:
        # operators may want to keep the discovered skills around after
        # retiring a source.
        repo_snapshot = {
            "name": row.name,
            "repo_url": row.repo_url,
            "git_ref": row.git_ref,
            "discovered_skill_count": row.discovered_skill_count,
        }
        db.delete(row)
        db.commit()
        audit.log(
            action="skill_source_delete",
            db=db,
            admin=admin_row,
            tenant_id=tenant_id,
            resource_type="skill_source",
            resource_id=source_id,
            details=repo_snapshot,
        )

    # ------------------------------------------------------------------
    # Manual refresh (operator → "sync now")
    # ------------------------------------------------------------------

    @router.post(
        "/{source_id}/refresh",
        response_model=SkillSourceResponse,
        summary="Trigger an immediate sync (clears last_attempted_at)",
    )
    def refresh_source(
        source_id: str,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> SkillSourceResponse:
        admin_row, tenant_id, _ = admin_and_tenant
        row = (
            db.query(SkillSource)
            .filter(
                and_(SkillSource.id == source_id, SkillSource.tenant_id == tenant_id)
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="skill source not found")
        # Reset the row to `idle` and clear last_attempted_at as a
        # belt-and-braces measure (so even if the immediate-sync
        # below fails to schedule, the next periodic tick picks
        # this up too).
        row.last_attempted_at = None
        if row.status != SYNC_STATUS_IDLE:
            row.status = SYNC_STATUS_IDLE
        db.commit()
        db.refresh(row)
        audit.log(
            action="skill_source_refresh",
            db=db,
            admin=admin_row,
            tenant_id=tenant_id,
            resource_type="skill_source",
            resource_id=row.id,
            details={"action": "refresh_requested"},
        )
        # Fire-and-forget an immediate sync on the running event
        # loop so the operator sees the row flip to `syncing` within
        # a second of clicking, instead of waiting up to 60s for the
        # periodic scheduler tick. The scheduler is in-process now
        # (it used to be a separate Rust container) so we can call
        # it directly. The task respects the same CAS in
        # `_claim_source` that prevents double-processing if the
        # periodic tick fires at the same instant.
        try:
            from app.services.skill_source_sync.scheduler import (
                trigger_sync_now,
            )
            trigger_sync_now(row.id)
        except Exception as e:  # noqa: BLE001 — never fail the HTTP call
            logger.warning(
                "skill_source: failed to schedule immediate sync — "
                "row will still be picked up by the next periodic tick",
                context={"source_id": row.id, "error": str(e)[:200]},
            )
        return _skill_source_to_response(row)

    # ------------------------------------------------------------------
    # Sync ingest (scheduler → control plane)
    # ------------------------------------------------------------------

    @router.post(
        "/{source_id}/sync-ingest",
        response_model=SkillSourceIngestSummary,
        summary="Scheduler payload — discovered skills for this source",
    )
    def sync_ingest(
        source_id: str,
        payload: SkillSourceIngestRequest,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
    ) -> SkillSourceIngestSummary:
        _, tenant_id, _ = admin_and_tenant
        row = (
            db.query(SkillSource)
            .filter(
                and_(SkillSource.id == source_id, SkillSource.tenant_id == tenant_id)
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="skill source not found")

        summary = _ingest_discovered_skills(
            db=db,
            source=row,
            payload=payload,
            logger=logger,
        )

        # Stamp the row with the success result.
        row.finish_sync(
            commit_sha=payload.commit_sha,
            discovered_count=summary.created + summary.updated + summary.unchanged,
        )
        db.commit()

        logger.info(
            "skill_source ingest complete",
            context={
                "source_id": row.id,
                "tenant_id": tenant_id,
                "commit": payload.commit_sha,
                "created": summary.created,
                "updated": summary.updated,
                "unchanged": summary.unchanged,
                "skipped": summary.skipped,
                "errors": len(summary.errors),
            },
        )
        return summary

    # ------------------------------------------------------------------
    # Sync error (scheduler → control plane, when sync failed early)
    # ------------------------------------------------------------------

    @router.post(
        "/{source_id}/sync-error",
        response_model=SkillSourceResponse,
        summary="Scheduler reports a sync failure",
    )
    def sync_error(
        source_id: str,
        payload: SkillSourceSyncErrorRequest,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.AGENT_SKILLS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AGENT_SKILL_UPDATE)
        ),
    ) -> SkillSourceResponse:
        _, tenant_id, _ = admin_and_tenant
        row = (
            db.query(SkillSource)
            .filter(
                and_(SkillSource.id == source_id, SkillSource.tenant_id == tenant_id)
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="skill source not found")
        row.record_error(payload.message)
        db.commit()
        db.refresh(row)
        logger.warning(
            "skill_source sync failed",
            context={
                "source_id": row.id,
                "tenant_id": tenant_id,
                "message": payload.message[:200],
            },
        )
        return _skill_source_to_response(row)

    return router


# ----------------------------------------------------------------------
# Ingest logic (extracted so tests can exercise the merge path without
# spinning up the FastAPI test client)
# ----------------------------------------------------------------------


def refused_line(skill_path: str, error: str) -> str:
    """How a skill refused as too large is reported on its source."""
    return f"{skill_path}: refused: {agent_skill_size.REASON}: {error}"


def _ingest_discovered_skills(
    *,
    db: Session,
    source: SkillSource,
    payload: SkillSourceIngestRequest,
    logger: BrutorLogger,
    refused: Sequence[str] = (),
) -> SkillSourceIngestSummary:
    """Match discovered skills against existing rows by (source_repo_id,
    source_path); insert new, update changed, leave unchanged ones alone.

    # Why we match on source_path, not skill.name

    Two skills in two different folders can carry the same name; only
    `source_path` is structurally unique within a repo. The skill.name
    is still validated for uniqueness within the tenant via the table's
    UNIQUE(tenant_id, name, version) constraint — but for repo→DB
    reconciliation, path is the right key.

    A discovered skill past the per-skill limit (`SKILL_MAX_BYTES`) is
    refused (`<path>: refused: too_large: ...`), as are the skills the
    GitHub sync refused before downloading them (`refused`, the same
    lines).
    """
    summary = SkillSourceIngestSummary(
        source_id=source.id,
        commit_sha=payload.commit_sha,
    )
    for line in refused:
        summary.errors.append(line)
        summary.skipped += 1

    # Bulk-load existing rows for this source.
    existing_rows: dict[str, AgentSkill] = {
        row.source_path: row
        for row in db.query(AgentSkill)
        .filter(
            and_(
                AgentSkill.tenant_id == source.tenant_id,
                AgentSkill.source_repo_id == source.id,
                AgentSkill.source_path.isnot(None),
            )
        )
        .all()
    }

    initial_status = (
        SkillStatus.PUBLISHED if source.auto_publish else SkillStatus.DRAFT
    )

    # Collected for the audit trail — operators want a record of which
    # skills a source sync materialised, with names + ids.
    created_skills: list[dict] = []
    updated_skills: list[dict] = []

    for discovered in payload.discovered:
        size = agent_skill_size.discovered_bytes(discovered.skill_md_body, discovered.files)
        if agent_skill_size.too_large(size):
            summary.errors.append(
                refused_line(discovered.skill_path, agent_skill_size.message(discovered.name, size))
            )
            summary.skipped += 1
            logger.warning(
                "skill_source ingest refused one skill",
                context={"source_id": source.id, "skill_path": discovered.skill_path, "reason": agent_skill_size.REASON},
            )
            continue
        try:
            row = existing_rows.get(discovered.skill_path)
            if row is None:
                new_skill = _create_skill_from_discovery(
                    db=db,
                    source=source,
                    commit_sha=payload.commit_sha,
                    discovered=discovered,
                    initial_status=initial_status,
                )
                summary.created += 1
                created_skills.append({
                    "id": new_skill.id,
                    "name": new_skill.name,
                    "path": discovered.skill_path,
                })
            else:
                changed = _update_skill_from_discovery(
                    row=row,
                    commit_sha=payload.commit_sha,
                    discovered=discovered,
                )
                if changed:
                    summary.updated += 1
                    updated_skills.append({
                        "id": row.id,
                        "name": row.name,
                        "path": discovered.skill_path,
                    })
                else:
                    summary.unchanged += 1
        except HTTPException as refusal:
            # The quota check refused a new skill. It is asked
            # before anything of the skill is written, so there is nothing
            # to roll back: the skill is reported and the others go on.
            summary.errors.append(
                f"{discovered.skill_path}: refused: "
                f"{TenantService.quota_refusal_reason(refusal)[:300]}"
            )
            summary.skipped += 1
            logger.warning(
                "skill_source ingest refused one skill",
                context={
                    "source_id": source.id,
                    "skill_path": discovered.skill_path,
                    "status": refusal.status_code,
                },
            )
        except Exception as exc:  # noqa: BLE001 — we want to keep ingesting
            db.rollback()
            summary.errors.append(
                f"{discovered.skill_path}: {type(exc).__name__}: {str(exc)[:200]}"
            )
            summary.skipped += 1
            logger.warning(
                "skill_source ingest skipped one skill",
                context={
                    "source_id": source.id,
                    "skill_path": discovered.skill_path,
                    "error": str(exc)[:500],
                },
            )

    db.commit()

    # Audit the ingest into the TENANT trail so it's visible in the
    # tenant's audit view alongside manual skill creates. Source sync
    # has no admin in the request path (the in-process scheduler is the
    # actor, or an operator-triggered refresh), so admin_id is NULL and
    # the synthetic actor + provenance live in `details`. Only emit when
    # something actually changed — a no-op poll shouldn't spam the trail.
    if created_skills or updated_skills or summary.errors:
        try:
            UnifiedAdminAuditLogger().log(
                action="agent_skill_source_ingest",
                db=db,
                admin_id=None,
                admin_type=AdminType.TENANT_ADMIN_TYPE,
                success=not summary.errors,
                resource_type=TenantResourceTypes.AGENT_SKILLS.value,
                resource_id=source.id,
                tenant_id=source.tenant_id,
                details={
                    "actor": f"skill-source-sync:{source.id}",
                    "source_id": source.id,
                    "source_name": source.name,
                    "repo_url": source.repo_url,
                    "commit_sha": payload.commit_sha,
                    "created_count": summary.created,
                    "updated_count": summary.updated,
                    "unchanged_count": summary.unchanged,
                    "skipped_count": summary.skipped,
                    "created_skills": created_skills,
                    "updated_skills": updated_skills,
                    "errors": summary.errors[:20],
                },
            )
        except Exception as audit_exc:  # noqa: BLE001 — never fail ingest on audit
            logger.warning(
                "skill_source ingest audit-log failed",
                context={"source_id": source.id, "error": str(audit_exc)[:300]},
            )

    return summary


def _create_skill_from_discovery(
    *,
    db: Session,
    source: SkillSource,
    commit_sha: str,
    discovered: DiscoveredSkill,
    initial_status: SkillStatus,
) -> AgentSkill:
    import hashlib

    body = discovered.skill_md_body
    skill_md_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    version = discovered.version or "1.0.0"

    # Name-collision adoption policy. The reconciler matches existing
    # rows by (source_repo_id, source_path) — but agent_skills has a
    # second UNIQUE constraint on (tenant_id, name, version) that
    # the path-keyed lookup can't see. Without this pre-check, a
    # naked INSERT would IntegrityError out and the operator would
    # have to manually delete the conflicting row.
    #
    # Three cases:
    #   1. Row exists, source_repo_id IS NULL → manually-authored
    #      skill that happens to share the name. ADOPT: claim it
    #      as belonging to this source and update in place.
    #   2. Row exists, source_repo_id == THIS source → the SKILL.md
    #      moved to a different path within the repo (rename /
    #      reorganise). Re-stamp source_path and update in place.
    #   3. Row exists, source_repo_id == DIFFERENT source → genuine
    #      conflict between two sources claiming the same name.
    #      Raise so the operator sees a clear error in the Sources
    #      tab; they need to rename one or retire the other source.
    collision: Optional[AgentSkill] = (
        db.query(AgentSkill)
        .filter(
            and_(
                AgentSkill.tenant_id == source.tenant_id,
                AgentSkill.name == discovered.name,
                AgentSkill.version == version,
            )
        )
        .first()
    )
    if collision is not None:
        existing_src = collision.source_repo_id
        if existing_src and existing_src != source.id:
            # Case 3 — foreign-source conflict; refuse to clobber.
            raise ValueError(
                f"name '{discovered.name}' v{version} is already owned "
                f"by source {existing_src!r}; rename the SKILL.md or "
                f"retire the other source to resolve"
            )
        # Cases 1 + 2 — adopt: stamp source columns and treat the
        # rest as an update.
        collision.source_repo_id = source.id
        collision.source_commit_sha = commit_sha
        collision.source_path = discovered.skill_path
        # Published skills are immutable even on adoption — body /
        # metadata stay frozen; we only re-stamp source columns so
        # subsequent re-syncs find the row via the path lookup.
        if collision.status != SkillStatus.PUBLISHED:
            _update_skill_from_discovery(
                row=collision,
                commit_sha=commit_sha,
                discovered=discovered,
            )
            _replace_skill_files(
                db=db, skill=collision, files=discovered.files
            )
        return collision

    # One more skill of the tenant's quota, asked as the create route asks
    # it and before the row is written. Raises the check's
    # HTTPException; `_ingest_discovered_skills` reports the skill.
    TenantService.check_quota(db, source.tenant_id, TenantResourceTypes.AGENT_SKILLS)

    skill = AgentSkill(
        id=f"skill-{ULID()}",
        tenant_id=source.tenant_id,
        name=discovered.name,
        description=discovered.description,
        version=version,
        license=discovered.license,
        compatibility=discovered.compatibility,
        allowed_tools=discovered.allowed_tools,
        skill_md_content=body,
        skill_md_hash=skill_md_hash,
        skill_metadata=discovered.skill_metadata,
        status=initial_status,
        enabled=True,
        source_repo_id=source.id,
        source_commit_sha=commit_sha,
        source_path=discovered.skill_path,
        created_by="skill_source_sync",
    )
    db.add(skill)
    db.flush()
    _replace_skill_files(db=db, skill=skill, files=discovered.files)
    return skill


def _update_skill_from_discovery(
    *,
    row: AgentSkill,
    commit_sha: str,
    discovered: DiscoveredSkill,
) -> bool:
    """Return True if anything actually changed. Published skills are
    treated as immutable — updates fall through to a no-op so operators
    don't silently mutate something in active use. The admin UI flags
    these so the operator can promote a new version manually.
    """
    if row.status == SkillStatus.PUBLISHED:
        # Snapshot only — record the commit we saw so the UI can show
        # "upstream has changes; publish a new version to pick them up".
        row.source_commit_sha = commit_sha
        return False

    import hashlib

    body = discovered.skill_md_body
    new_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    changed = False
    if row.skill_md_hash != new_hash:
        row.skill_md_content = body
        row.skill_md_hash = new_hash
        changed = True
    for field, new_val in (
        ("description", discovered.description),
        ("license", discovered.license),
        ("compatibility", discovered.compatibility),
        ("allowed_tools", discovered.allowed_tools),
        ("skill_metadata", discovered.skill_metadata),
    ):
        if getattr(row, field) != new_val:
            setattr(row, field, new_val)
            changed = True
    if discovered.version and row.version != discovered.version:
        row.version = discovered.version
        changed = True
    # Commit SHA always moves forward to track the most recent sync,
    # but a commit-SHA-only diff is NOT operator-meaningful — the upstream
    # repo was re-synced but the actual skill payload (body + metadata)
    # didn't change. Tag this as "unchanged" so the per-source summary
    # accurately tells the operator how many skills actually evolved.
    if row.source_commit_sha != commit_sha:
        row.source_commit_sha = commit_sha

    return changed


def _replace_skill_files(
    *,
    db: Session,
    skill: AgentSkill,
    files: List,
) -> None:
    """Replace the skill's resource/script/asset rows with the
    discovered set. Cascade-delete-on-skill takes care of cleanup if
    the skill itself is later removed; here we just diff the live set
    against the discovered set.

    Note: kept as a single replace-all rather than a per-row diff
    because skills are small and the cost is negligible. Optimise if
    profiling shows otherwise.
    """
    # Lazy imports avoid circular dependencies at module load.
    from app.constants.agent_skill import (
        AssetType,
        ScriptExecutionMode,
        ScriptLanguage,
        SkillResourceType,
    )
    from app.database.models.agent_skill_asset import AgentSkillAsset
    from app.database.models.agent_skill_resource import AgentSkillResource
    from app.database.models.agent_skill_script import AgentSkillScript
    import hashlib

    # Wipe existing — small fan-out, simpler than per-row reconciliation.
    db.query(AgentSkillResource).filter(
        AgentSkillResource.skill_id == skill.id
    ).delete(synchronize_session=False)
    db.query(AgentSkillScript).filter(
        AgentSkillScript.skill_id == skill.id
    ).delete(synchronize_session=False)
    db.query(AgentSkillAsset).filter(
        AgentSkillAsset.skill_id == skill.id
    ).delete(synchronize_session=False)

    # Derive the skill's source-path directory so we can compute
    # filenames RELATIVE to the kind-subdir (scripts/, references/,
    # assets/). Without this, two siblings at different depths but
    # the same basename (e.g. `scripts/foo/__init__.py` and
    # `scripts/bar/__init__.py`) collide on the
    # UNIQUE(skill_id, filename) constraint — the Anthropic skills
    # repo trips this constantly because each Python sub-package has
    # its own __init__.py.
    skill_dir = ""
    if skill.source_path and "/" in skill.source_path:
        skill_dir = skill.source_path.rsplit("/", 1)[0]

    for f in files:
        content = f.content
        h = hashlib.sha256(
            content.encode("utf-8") if isinstance(content, str) else content
        ).hexdigest()

        # Filename preserves nesting relative to the on-disk subdir
        # (scripts/ / references/ / assets/ / templates/), detected from
        # the path — independent of the possibly-demoted f.kind.
        rel_filename = _relative_filename(f.path, skill_dir)

        if f.kind == "script":
            lang = _infer_script_language(f.path) or ScriptLanguage.BASH
            db.add(
                AgentSkillScript(
                    id=f"ass-{ULID()}",
                    tenant_id=skill.tenant_id,
                    skill_id=skill.id,
                    filename=rel_filename,
                    description=None,
                    script_content=content,
                    script_hash=h,
                    language=lang,
                    execution_mode=ScriptExecutionMode.SANDBOX,
                )
            )
        elif f.kind == "asset":
            db.add(
                AgentSkillAsset(
                    id=f"asa-{ULID()}",
                    tenant_id=skill.tenant_id,
                    skill_id=skill.id,
                    filename=rel_filename,
                    description=None,
                    content=content,
                    content_hash=h,
                    mime_type=f.mime_type,
                    encoding=f.encoding,
                    asset_type=AssetType.DATA,
                )
            )
        elif f.kind == "reference":
            db.add(
                AgentSkillResource(
                    id=f"asr-{ULID()}",
                    tenant_id=skill.tenant_id,
                    skill_id=skill.id,
                    filename=rel_filename,
                    resource_type=SkillResourceType.REFERENCE,
                    content=content,
                    content_hash=h,
                    mime_type=f.mime_type,
                    size_bytes=len(content) if isinstance(content, str) else len(content),
                )
            )
        # kind == "skill_md" is the SKILL.md itself — body lives on the
        # parent row, no separate row to insert.


def _basename(path: str) -> str:
    # Trim path to the final segment; cap at 255 so a hostile
    # multi-MB-deep path can't blow the filename column. Retained
    # for back-compat callers; new code should use
    # _relative_filename() so nested __init__.py files don't
    # collide on the (skill_id, filename) UNIQUE constraint.
    base = path.rsplit("/", 1)[-1]
    return base[:255]


def _relative_filename(path: str, skill_dir: str, _kind_prefix: str = "") -> str:
    """Compute the bundled file's stored `filename` = its path relative
    to the SKILL ROOT, preserving the bucket directory and any nesting.

    Examples (skill_dir='skills/docx'):
        'skills/docx/scripts/foo.py'               → 'scripts/foo.py'
        'skills/docx/scripts/office/x/__init__.py' → 'scripts/office/x/__init__.py'
        'skills/docx/templates/comments.xml'       → 'templates/comments.xml'
        'skills/docx/agents/grader.md'             → 'agents/grader.md'

    Storing the full root-relative path (matching the ZIP importer's
    convention) means the export endpoint reconstructs the original
    directory layout faithfully — including non-canonical directories
    like `agents/` or `evals/` that the spec permits. The
    UNIQUE(skill_id, filename) constraint is satisfied because two
    files at different paths now keep distinct names (the old
    bucket-relative form collided `scripts/a/__init__.py` with
    `scripts/b/__init__.py`).

    `_kind_prefix` is retained for signature back-compat but ignored.
    Falls back to the bare basename for a flat-layout repo with no
    skill_dir. Cap at 255 to match the column length."""
    if skill_dir and path.startswith(f"{skill_dir}/"):
        return path[len(skill_dir) + 1:][:255]
    return path.rsplit("/", 1)[-1][:255]


def _infer_script_language(path: str):
    from app.constants.agent_skill import ScriptLanguage

    lower = path.lower()
    if lower.endswith(".py"):
        return ScriptLanguage.PYTHON
    if lower.endswith(".ts"):
        return ScriptLanguage.TYPESCRIPT
    if lower.endswith(".js") or lower.endswith(".mjs"):
        return ScriptLanguage.JAVASCRIPT
    if lower.endswith(".sh") or lower.endswith(".bash"):
        return ScriptLanguage.BASH
    return None
