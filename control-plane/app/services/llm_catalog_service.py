# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog service

Business logic for browsing the GLOBAL `llm_catalog` and importing an
entry into a tenant as a configured `llm_model` instance.

All methods are sync, take an explicit `db: Session` (the endpoint hands
over its `Depends(get_db)` session — this manager never opens or commits
its own transaction; the commit is owned by `get_db`).

`list_catalog` / `get_entry` are tenant-agnostic (the catalog is global).
`import_to_model` is tenant-scoped: it creates a real `llm_model` row in
the caller's tenant, resolving `provider_id` from `provider_key` (lazy-
creating the provider row when the tenant hasn't provisioned it yet) and
copying the catalog's descriptive defaults onto the instance.

Domain errors raise `LlmCatalogError(code, message)`; the endpoint layer
maps `code` to an HTTP status (404 / 409 / 400).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.init_llms import build_model_config
from app.database.models.llm_catalog import (
    CATALOG_MODES,
    CATALOG_SOURCE_CUSTOM,
    LlmCatalog,
)
from app.database.models.llm_model import LLModel


class LlmCatalogError(Exception):
    """Service-level error. Caught by the endpoint layer and translated
    into an HTTP 4xx response with a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class LlmCatalogService:
    """Browse the global catalog + import entries into a tenant."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("llm-catalog-service")

    # =====================================================================
    # Read (global — no tenant scope)
    # =====================================================================

    def list_catalog(
        self,
        db: Session,
        *,
        provider_key: Optional[str] = None,
        mode: Optional[str] = None,
        recommended: Optional[bool] = None,
        q: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[LlmCatalog], int]:
        """Paginated, filtered browse. Returns (rows, total).

        `q` is a case-insensitive substring match on display_name OR
        model_name. `mode` is validated against the catalog vocabulary so
        a typo returns a tidy 400 rather than silently matching nothing.
        """
        where = []
        if provider_key is not None:
            where.append(LlmCatalog.provider_key == provider_key.lower())
        if mode is not None:
            if mode not in CATALOG_MODES:
                raise LlmCatalogError(
                    "invalid_value",
                    f"Invalid mode {mode!r}; expected one of {sorted(CATALOG_MODES)}",
                )
            where.append(LlmCatalog.mode == mode)
        if recommended is not None:
            where.append(LlmCatalog.recommended == recommended)
        if q:
            needle = f"%{q.lower()}%"
            where.append(
                func.lower(LlmCatalog.display_name).like(needle)
                | func.lower(LlmCatalog.model_name).like(needle)
            )

        clause = and_(*where) if where else None

        total_stmt = select(func.count()).select_from(LlmCatalog)
        if clause is not None:
            total_stmt = total_stmt.where(clause)
        total = db.execute(total_stmt).scalar_one()

        rows_stmt = select(LlmCatalog)
        if clause is not None:
            rows_stmt = rows_stmt.where(clause)
        rows_stmt = (
            rows_stmt.order_by(
                LlmCatalog.provider_key.asc(),
                LlmCatalog.display_name.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = db.execute(rows_stmt).scalars().all()
        return list(rows), int(total or 0)

    def get_entry(self, db: Session, catalog_id: str) -> LlmCatalog:
        """Fetch a catalog entry. Raises 404 when missing."""
        entry = db.get(LlmCatalog, catalog_id)
        if entry is None:
            raise LlmCatalogError(
                "not_found",
                f"LLM catalog entry {catalog_id!r} not found",
            )
        return entry

    # =====================================================================
    # Write (global curation — system-admin only, no tenant scope)
    # =====================================================================

    # Per-model import-time default columns. Copied verbatim
    # onto an imported llm_model so media whitelists / per-unit costs /
    # reasoning behaviour / intrinsic facts survive the catalog → instance
    # import. Single source of truth reused by create/update + import.
    _IMPORT_DEFAULT_FIELDS = (
        "cost_per_image",
        "supported_image_sizes",
        "supported_image_qualities",
        "max_images_per_request",
        "cost_per_audio_char",
        "cost_per_audio_minute",
        "supported_voices",
        "supported_audio_formats",
        "supported_input_formats",
        "supported_response_formats",
        "max_audio_duration_seconds",
        "cost_per_video_second",
        "supported_video_resolutions",
        "supported_video_durations",
        "max_video_duration_seconds",
        "thinking_enabled",
        "additional_drop_params",
        "parameters",
        "tags",
        "retire_date",
    )

    # The mutable descriptive columns an operator may set on create/update.
    _MUTABLE_FIELDS = (
        "display_name",
        "description",
        "mode",
        "capabilities",
        "context_window",
        "max_output_tokens",
        "default_cost_per_input_token",
        "default_cost_per_output_token",
        "icon_url",
        "recommended",
        "deprecated",
    ) + _IMPORT_DEFAULT_FIELDS

    def create_entry(
        self,
        db: Session,
        *,
        provider_key: str,
        model_name: str,
        display_name: str,
        description: Optional[str] = None,
        mode: str,
        capabilities: Optional[Dict[str, Any]] = None,
        context_window: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        default_cost_per_input_token: Optional[float] = None,
        default_cost_per_output_token: Optional[float] = None,
        icon_url: Optional[str] = None,
        recommended: bool = False,
        deprecated: bool = False,
        **extra: Any,
    ) -> LlmCatalog:
        """Create a custom global catalog entry.

        `extra` may carry any of the per-model import-default columns
        (`_IMPORT_DEFAULT_FIELDS`) — applied to the new row. Unknown keys
        are ignored.

        Validates `mode` against the catalog vocabulary and rejects a
        duplicate `(provider_key, model_name)` with a 409. The id follows
        the `llmcat-{ULID}` convention and `source="custom"` (distinct from
        the deterministic ids of builtin/seeded rows)."""
        pkey = (provider_key or "").strip().lower()
        mname = (model_name or "").strip()
        if not pkey:
            raise LlmCatalogError("invalid_value", "provider_key is required")
        if not mname:
            raise LlmCatalogError("invalid_value", "model_name is required")
        if mode not in CATALOG_MODES:
            raise LlmCatalogError(
                "invalid_value",
                f"Invalid mode {mode!r}; expected one of {sorted(CATALOG_MODES)}",
            )

        existing = db.execute(
            select(LlmCatalog).where(
                LlmCatalog.provider_key == pkey,
                LlmCatalog.model_name == mname,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise LlmCatalogError(
                "duplicate",
                f"A catalog entry for provider {pkey!r} model {mname!r} "
                f"already exists ({existing.id})",
            )

        entry = LlmCatalog(
            id=f"llmcat-{ULID()}",
            provider_key=pkey,
            model_name=mname,
            display_name=display_name,
            description=description,
            mode=mode,
            capabilities=capabilities,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            default_cost_per_input_token=default_cost_per_input_token,
            default_cost_per_output_token=default_cost_per_output_token,
            icon_url=icon_url,
            recommended=recommended,
            deprecated=deprecated,
            source=CATALOG_SOURCE_CUSTOM,
        )
        # Apply any import-default fields supplied by the caller.
        for field in self._IMPORT_DEFAULT_FIELDS:
            if field in extra:
                setattr(entry, field, extra[field])
        db.add(entry)
        db.flush()
        self.logger.info(
            f"Created custom catalog entry {entry.id} "
            f"(provider={pkey}, model_name={mname})"
        )
        return entry

    def update_entry(
        self, db: Session, catalog_id: str, **partial: Any
    ) -> LlmCatalog:
        """Partially update a catalog entry. Only the fields actually
        supplied (present in `partial`) are written; everything else is
        left untouched. Raises 404 when the entry is missing and 400 on an
        invalid `mode`.

        `provider_key` / `model_name` are intentionally NOT mutable here:
        changing them would break `(provider_key, model_name)` provenance on
        already-imported `llm_model` rows."""
        entry = self.get_entry(db, catalog_id)

        if "mode" in partial:
            mode = partial["mode"]
            if mode not in CATALOG_MODES:
                raise LlmCatalogError(
                    "invalid_value",
                    f"Invalid mode {mode!r}; expected one of "
                    f"{sorted(CATALOG_MODES)}",
                )

        changed = []
        for field in self._MUTABLE_FIELDS:
            if field in partial:
                setattr(entry, field, partial[field])
                changed.append(field)
        db.flush()
        self.logger.info(
            f"Updated catalog entry {entry.id} (fields={changed})"
        )
        return entry

    def delete_entry(self, db: Session, catalog_id: str) -> None:
        """Delete a catalog entry. Raises 404 when missing."""
        entry = self.get_entry(db, catalog_id)
        db.delete(entry)
        db.flush()
        self.logger.info(f"Deleted catalog entry {catalog_id}")

    # =====================================================================
    # Write (tenant-scoped import)
    # =====================================================================

    def import_to_model(
        self,
        db: Session,
        tenant_id: str,
        catalog_id: str,
        *,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> LLModel:
        """Import a catalog entry into the tenant as a configured
        `llm_model` instance.

        Resolves `provider_id` from the catalog's `provider_key` (lazy-
        creating the provider row when the tenant hasn't provisioned it),
        copies the catalog's descriptive defaults (model_name, display
        name, mode, costs, context window, capabilities) onto a fresh
        instance, stamps `catalog_id` for provenance and `enabled=True`
        (unless overridden). The instance id follows the existing
        `llm-{ULID}` convention.

        `overrides` may carry any LLModel column (e.g. `name`, `enabled`,
        `api_key`) — applied last, after the catalog defaults.
        """
        if not tenant_id:
            raise LlmCatalogError("invalid_tenant", "tenant_id is required")

        entry = self.get_entry(db, catalog_id)
        overrides = dict(overrides or {})

        # A template (`requires_api_url`) becomes a row only with the
        # customer's own address: the gateway refuses a row without one.
        # Before anything is written.
        from app.services.llm.destination import destination_problem

        problem = destination_problem(
            entry.provider_key.lower(),
            overrides.get("mode", entry.mode),
            entry.model_name,
            overrides.get("api_url"),
        )
        if problem:
            raise LlmCatalogError("api_url_required", problem)

        # A cloud entry (Amazon Bedrock, Vertex AI): the import carries no
        # cloud credential, and the gateway refuses an enabled cloud row
        # without its own (501 provider_credential_missing). It becomes a
        # row that is switched off, to be given its credential and enabled
        # on the model itself; asked to be enabled here, or given an
        # `api_key` the gateway reads nowhere on such a row, it is refused.
        from app.services.llm import provider_config as provider_config_rules

        cloud = entry.provider_key.lower() in provider_config_rules.CLOUD_PROVIDERS
        if cloud:
            problem = provider_config_rules.api_key_problem(
                entry.provider_key.lower(), "set" if overrides.get("api_key") else None
            )
            if not problem and overrides.get("enabled"):
                problem = provider_config_rules.enabled_row_problem(
                    entry.provider_key.lower(), entry.model_name, overrides.get("api_url"), None
                )
            if problem:
                raise LlmCatalogError("api_url_required", problem)
            overrides["enabled"] = False

        # The model quota counts enabled rows: an import that is switched
        # on is one more of them, asked as a creation is and before anything
        # is written. The check refuses by raising its HTTPException.
        if overrides.get("enabled", True):
            from app.constants.tenant import TenantResourceTypes
            from app.services.tenant.tenant_service import TenantService

            TenantService.check_quota(db, tenant_id, TenantResourceTypes.LLM_MODELS)

        # Resolve provider_id from the catalog's provider_key. Reuse the
        # same lazy-create path the admin create endpoint uses so a tenant
        # importing a model for a not-yet-provisioned provider still lands.
        from app.database.init_llm_providers import upsert_provider_for_key
        from app.database.models.llm_provider import LlmProvider

        provider = db.execute(
            select(LlmProvider).where(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.key == entry.provider_key.lower(),
            )
        ).scalar_one_or_none()
        if provider is None:
            provider = upsert_provider_for_key(
                db, tenant_id=tenant_id, key=entry.provider_key.lower(),
                is_system=True,
            )
        provider_id = provider.id

        # Build a complete model config off the provider template + the
        # catalog's descriptive fields so per-provider defaults (auth_type,
        # drop_params, timeouts, capability flags) match a normal seed.
        model_overrides: Dict[str, Any] = {
            "model_name": entry.model_name,
            "name": entry.display_name,
            "description": entry.description,
            "mode": entry.mode,
            "cost_per_input_token": entry.default_cost_per_input_token,
            "cost_per_output_token": entry.default_cost_per_output_token,
            "capabilities": entry.capabilities,
            "enabled": True,
        }
        if entry.max_output_tokens:
            model_overrides["default_max_tokens"] = entry.max_output_tokens

        # Copy the per-model import-default columns (media whitelists,
        # per-unit costs, reasoning/drop-param behaviour, intrinsic facts)
        # onto the instance when the catalog carries them. Only non-None
        # values override the provider-template default so an entry that
        # doesn't declare a field falls back to the template as before.
        for field in self._IMPORT_DEFAULT_FIELDS:
            value = getattr(entry, field, None)
            if value is not None:
                model_overrides[field] = value

        config = build_model_config(entry.provider_key, model_overrides)

        # build_model_config injects a fresh id + a `provider` slug; drop
        # both — we use an llm- id and the resolved provider_id FK instead.
        config.pop("id", None)
        config.pop("provider", None)
        # `tags` is a merged list from the template; keep it.

        # Surface catalog capability flags as the LLModel boolean columns
        # so the admin UI badges render without parsing JSON.
        caps = entry.capabilities or {}
        if isinstance(caps, dict):
            if caps.get("supports_vision"):
                config["supports_vision"] = True
            if caps.get("supports_function_calling"):
                config["supports_function_calling"] = True
            if caps.get("supports_batch"):
                config["supports_batch"] = True

        # Apply caller overrides last.
        config.update(overrides)

        if (config.get("api_url") or "").strip():
            config["api_url_auto_discovered"] = False

        model = LLModel(
            id=f"llm-{ULID()}",
            tenant_id=tenant_id,
            provider_id=provider_id,
            catalog_id=entry.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            **config,
        )
        db.add(model)
        db.flush()

        self.logger.info(
            f"Imported catalog entry {entry.id} → llm_model {model.id} "
            f"in tenant {tenant_id} (provider={entry.provider_key}, "
            f"model_name={entry.model_name})"
        )
        return model


__all__ = [
    "LlmCatalogService",
    "LlmCatalogError",
]
