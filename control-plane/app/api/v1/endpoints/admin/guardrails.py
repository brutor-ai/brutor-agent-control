# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrails Admin Endpoints
Multi-Tenant with RBAC and Audit Logging

Per-tenant guardrail configuration controlling content filtering across
all three proxy surfaces (LLM, MCP, Agent Skills).

Each tenant gets one GuardrailConfig row (singleton per tenant — the
endpoints are upsert-style rather than traditional CRUD).
"""

from datetime import datetime, timedelta, timezone
from typing import Union, Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.database import get_db
from app.database.models.guardrail_config import (
    GuardrailConfig,
    ALL_SURFACES,
    INPUT_SURFACES,
    LLM_MCP_SURFACES,
    DEFAULT_SECRETS_CATEGORIES,
    AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES,
    AUDIT_RECOMMENDED_TOXIC_CONTENT_SURFACES,
    AUDIT_RECOMMENDED_WIDE_SURFACES,
)
from app.database.models.guardrail_break_glass import (
    DEFAULT_BREAK_GLASS_MINUTES,
    MAX_BREAK_GLASS_HOURS,
    GuardrailBreakGlass,
)
from app.database.models.resource_group import ResourceGroup
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger,
    require_tenant_resource_access,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.guardrail import (
    BreakGlassActivateRequest,
    GuardrailBuiltinConfig,
    GuardrailConfigUpdate,
    GuardrailConfigCreateRequest,
    GuardrailGroupAssociationRequest,
    GuardrailGroupInfo,
    GuardrailTestRequest,
    GuardrailConfigDetailResponse,
    GuardrailConfigResponse,
    GuardrailConfigListResponse,
    GuardrailSurfaceResponse,
    GuardrailBuiltinResponse,
    GuardrailPiiResponse,
    GuardrailPromptInjectionResponse,
    GuardrailJailbreakResponse,
    GuardrailToxicContentResponse,
    GuardrailSecretsResponse,
    GuardrailProviderResponse,
    GuardrailTestResponse,
    GuardrailViolationResponse,
    GuardrailProviderListResponse,
    GuardrailProviderInfo,
    GuardrailProviderTestRequest,
    GuardrailProviderTestResponse,
)
from app.services.guardrails.guardrails_service import GuardrailsService
from app.services.cache_invalidation import invalidate_proxy_cache
from app.services.oauth.encryption_service import EncryptionService, has_ciphertext_shape
from app.services.guardrails import provider_secrets
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def create_guardrails_router(
        guardrails_service: GuardrailsService,
        logger: BrutorLogger,
        encryption_service: "EncryptionService" = None,
) -> APIRouter:
    """
    Factory function to create Guardrails admin router with full tenant isolation

    Args:
        guardrails_service: Guardrails service instance (shared singleton)
        logger: Logger instance
        encryption_service: Used to encrypt external-provider ``api_key`` at
            rest (same service that encrypts LLM model api_key). The Rust
            proxy decrypts on load via the matching EncryptionService.

    Returns:
        Configured APIRouter with all guardrails admin endpoints
    """

    router = APIRouter(prefix="/admin/guardrails", tags=["Guardrails Admin"])

    def _encrypt_provider_api_keys(new_providers, old_providers, tenant_id):
        """Encrypt each external-provider ``api_key`` at rest in place.

        Same EncryptionService that protects LLM model api_key; the Rust
        proxy decrypts on load. Per entry:
          * empty/None  → preserve the saved (already-encrypted) value —
            the GET response strips api_key, so an unchanged provider
            arrives without it; don't clobber it to null.
          * already ciphertext (by its shape) OR a value identical to the stored one →
            leave as-is (idempotent re-save / policy round-trip; never
            double-encrypt).
          * fresh plaintext → encrypt.

        No-op when no EncryptionService was provided (test harnesses);
        the value is then left untouched rather than silently dropped.
        """
        if not new_providers:
            return
        # Index existing entries by provider id to recover an omitted key.
        prev_by_provider = {}
        for e in (old_providers or []):
            if isinstance(e, dict) and e.get("provider"):
                prev_by_provider[e["provider"].lower()] = e
        for entry in new_providers:
            if not isinstance(entry, dict):
                continue
            pid = (entry.get("provider") or "").lower()
            key = entry.get("api_key")
            prev = prev_by_provider.get(pid) or {}
            if not key:
                # Preserve the previously-stored (encrypted) value.
                entry["api_key"] = prev.get("api_key")
                continue
            if encryption_service is None:
                continue
            # Idempotent: leave an unchanged value, and one that has the
            # shape of ciphertext, alone. Decided by shape and not by
            # trying to decrypt: a value encrypted under another key does
            # not decrypt and is still not clear text.
            if key == prev.get("api_key") or has_ciphertext_shape(key):
                continue
            entry["api_key"] = encryption_service.encrypt(key)

    def _store_ready_providers(new_providers, old_providers, tenant_id):
        """`new_providers` as they are stored, in place: each entry with
        the secrets the request left out (no response carries them), a
        Bedrock entry complete if it is enabled (400
        `guardrail_provider_incomplete`), every secret encrypted (503
        where there is nothing to encrypt with)."""
        provider_secrets.keep_omitted(new_providers, old_providers)
        issues = provider_secrets.bedrock_problems(new_providers)
        if issues:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "guardrail_provider_incomplete",
                    "message": "Selected external providers are missing configuration.",
                    "issues": issues,
                },
            )
        # No secret is stored in clear: without an
        # encryption service a provider that carries one is not saved.
        if encryption_service is None and provider_secrets.has_secret(new_providers):
            raise HTTPException(
                status_code=503,
                detail=(
                    "A guardrail provider's credential cannot be stored: this control plane has no "
                    "encryption service (ENCRYPTION_KEY), and a credential is never stored in clear. "
                    "Nothing was saved."
                ),
            )
        _encrypt_provider_api_keys(new_providers, old_providers, tenant_id)
        # A Bedrock entry's secret access key and session token, as
        # `api_key` is; the gateway decrypts them on load.
        if encryption_service is not None:
            provider_secrets.encrypt_config_secrets(
                new_providers,
                lambda value, column: encryption_service.encrypt(value),
                has_ciphertext_shape,
            )

    def _provider_present_with_key(
        providers: Optional[list],
        provider_aliases: tuple[str, ...],
    ) -> bool:
        """
        Return True iff the request's `providers` list (whatever the
        admin sent on save) contains an enabled entry matching one of
        ``provider_aliases`` AND that entry carries a non-empty
        ``api_key``. The orchestrator's runtime build will refuse to
        instantiate the detector otherwise — this validator front-loads
        that to save-time so the admin sees the error before the policy
        becomes live.
        """
        if not providers:
            return False
        for entry in providers:
            data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
            if data.get("enabled") is False:
                continue
            if data.get("provider") in provider_aliases:
                key = data.get("api_key") or ""
                if isinstance(key, str) and key.strip():
                    return True
        return False

    def _enabled_provider_entries(
        providers: Optional[list],
        provider_id: str,
    ) -> List[dict]:
        """All enabled `providers[]` entries of a given provider id, as
        plain dicts. Used to validate the standalone providers (Bedrock,
        Custom Webhook) which have no per-check selector field."""
        if not providers:
            return []
        out: List[dict] = []
        for entry in providers:
            data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
            if data.get("enabled") is False:
                continue
            if data.get("provider") == provider_id:
                out.append(data)
        return out

    def _provider_present_with_endpoint(
        providers: Optional[list],
        provider_aliases: tuple[str, ...],
    ) -> bool:
        """Like ``_provider_present_with_key`` but for providers that
        authenticate by reachable URL rather than an api_key (Presidio
        is self-hosted — it needs an ``endpoint``, not a key)."""
        if not providers:
            return False
        for entry in providers:
            data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
            if data.get("enabled") is False:
                continue
            if data.get("provider") in provider_aliases:
                endpoint = data.get("endpoint") or ""
                if isinstance(endpoint, str) and endpoint.strip():
                    return True
        return False

    def _validate_provider_completeness(
        builtin: Optional["GuardrailBuiltinConfig"],
        providers: Optional[list],
    ) -> None:
        """
        Cross-field check: when an external provider is selected for a
        check, the corresponding ``providers`` entry must exist and
        carry an ``api_key``. Raises HTTPException(400) on failure.

        Runs in addition to the field-level Pydantic validators on
        ``GuardrailBuiltinConfig`` (allowed enum values, threshold
        ranges, regex compile, etc.).
        """
        errors: List[str] = []

        # Per-check provider selectors live on `builtin`; the standalone
        # providers (Bedrock, Custom Webhook) are validated by their
        # presence in providers[] regardless of whether `builtin` was
        # supplied — so the builtin-dependent checks are guarded, but
        # the standalone loop below always runs.
        if builtin is not None:
            if builtin.prompt_injection_enabled and builtin.prompt_injection_provider == "lakera_guard":
                if not _provider_present_with_key(providers, ("lakera", "lakera_guard")):
                    errors.append(
                        "prompt_injection_provider='lakera_guard' requires an enabled "
                        "Lakera entry in providers[] with a non-empty api_key."
                    )

            if builtin.jailbreak_enabled and builtin.jailbreak_provider == "lakera_guard":
                if not _provider_present_with_key(providers, ("lakera", "lakera_guard")):
                    errors.append(
                        "jailbreak_provider='lakera_guard' requires an enabled "
                        "Lakera entry in providers[] with a non-empty api_key."
                    )

            # toxic_content default selector is openai_moderation (None
            # treated as default). built_in selector requires no key.
            tc_provider = builtin.toxic_content_provider or "openai_moderation"
            if builtin.toxic_content_enabled and tc_provider == "openai_moderation":
                if not _provider_present_with_key(providers, ("openai_moderation",)):
                    errors.append(
                        "toxic_content_provider='openai_moderation' requires an enabled "
                        "OpenAI Moderation entry in providers[] with a non-empty api_key. "
                        "Switch toxic_content_provider to 'built_in' for the wordlist fallback."
                    )

            # PII via Presidio — needs an enabled Presidio entry with an
            # endpoint (Presidio is self-hosted; it has no api_key).
            if builtin.pii_enabled and builtin.pii_provider == "presidio":
                if not _provider_present_with_endpoint(providers, ("presidio",)):
                    errors.append(
                        "pii_provider='presidio' requires an enabled Presidio entry in "
                        "providers[] with a non-empty endpoint (the Analyzer URL). "
                        "Leave pii_provider unset to use the built-in regex detector."
                    )

        # Standalone providers — Bedrock and Custom Webhook are wired by
        # their presence in providers[], not a per-check selector, so
        # every enabled entry is validated for its required config.
        # (A Bedrock entry is validated as it will be stored, with the
        # secrets the request left out: `_store_ready_providers`.)
        for entry in _enabled_provider_entries(providers, "custom"):
            endpoint = entry.get("endpoint") or ""
            if not (isinstance(endpoint, str) and endpoint.strip()):
                errors.append(
                    "An enabled Custom Webhook provider requires a non-empty "
                    "endpoint — the URL the gateway POSTs content to."
                )

        if errors:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "guardrail_provider_incomplete",
                    "message": "Selected external providers are missing configuration.",
                    "issues": errors,
                },
            )

    # =========================================================================
    # Get Configuration
    # =========================================================================

    @router.get(
        "/config",
        response_model=GuardrailConfigDetailResponse,
        summary="Get guardrail configuration",
        description="""
        Get the guardrail configuration for the current tenant.
        
        Returns the full configuration including surface toggles, built-in
        check settings, external provider configurations, and performance
        settings.
        
        If no configuration has been saved yet, returns sensible defaults
        (guardrails disabled — opt-in model).
        
        Tenant isolation: Always scoped to the authenticated admin's tenant.
        """
    )
    def get_guardrail_config(
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_READ))
    ):
        """Get tenant guardrail configuration"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            config = db.query(GuardrailConfig).filter(
                GuardrailConfig.tenant_id == tenant_id
            ).first()

            if not config:
                config = GuardrailConfig.default_config(tenant_id)

            logger.debug("Guardrail config retrieved", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "enabled": config.enabled,
            })

            return GuardrailConfigDetailResponse(
                config=_config_to_response(config)
            )

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to get guardrail config", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to get guardrail configuration"
            )

    # =========================================================================
    # Update Configuration
    # =========================================================================

    @router.patch(
        "/config",
        response_model=GuardrailConfigDetailResponse,
        summary="Update guardrail configuration",
        description="""
        Create or update the guardrail configuration for the current tenant.
        
        This is an upsert operation: if no configuration exists yet, one is
        created; otherwise the existing configuration is updated.
        
        Changes take effect within 60 seconds (config cache TTL), or
        immediately if you call GET /config after updating.
        
        Use the POST /test endpoint to verify behaviour before enabling.
        
        Tenant isolation: Always scoped to the authenticated admin's tenant.
        
        Surface toggles (per-mode):
        - chat_input / chat_output: chat completions / responses / messages
        - embeddings_input: documents before embedding
        - image_gen_input / image_gen_output: image generation
        - audio_tts_input / audio_tts_output: voice synthesis
        - audio_stt_input / audio_stt_output: transcription
        - video_gen_input / video_gen_output: video generation
        - batch_input / batch_output: async batch jobs
        - moderation_input: moderation calls (jailbreak / injection / toxic auto-bypassed)
        - mcp_input / mcp_output / mcp_registration: MCP tool calls + tool description scan
        - skill_input / skill_output: skill execution
        - a2a_inbound / a2a_outbound: A2A v1 message bodies
        """
    )
    def update_guardrail_config(
            update_data: GuardrailConfigUpdate,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create or update tenant guardrail configuration"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        # Provider-completeness gate. External providers
        # must carry an api_key when their selector is chosen.
        _validate_provider_completeness(update_data.builtin, update_data.providers)

        try:
            config = db.query(GuardrailConfig).filter(
                GuardrailConfig.tenant_id == tenant_id
            ).first()

            is_new = config is None
            if is_new:
                config = GuardrailConfig(
                    id=str(ULID()),
                    tenant_id=tenant_id,
                )
                db.add(config)

            # Track changes for audit
            changes = {}
            old_enabled = config.enabled

            # ── Identity fields ────────────────────────────────────────
            if update_data.name is not None:
                _set_if_changed(config, "name", update_data.name, changes)
            if update_data.description is not None:
                _set_if_changed(config, "description", update_data.description, changes)
            if update_data.is_default is not None:
                # If setting as default, clear other defaults for this tenant
                if update_data.is_default and not config.is_default:
                    db.query(GuardrailConfig).filter(
                        GuardrailConfig.tenant_id == tenant_id,
                        GuardrailConfig.id != config.id,
                        GuardrailConfig.is_default == True,
                    ).update({"is_default": False})
                _set_if_changed(config, "is_default", update_data.is_default, changes)
            if update_data.priority is not None:
                _set_if_changed(config, "priority", update_data.priority, changes)

            # Per-call scope filters. PATCH semantics:
            # explicit `null` clears the scope; absent field is
            # left untouched. We use model_dump(exclude_unset=True)
            # to detect deliberate presence-of-key vs absence.
            update_payload_dict = update_data.model_dump(exclude_unset=True)
            if "scope_mcp_server_id" in update_payload_dict:
                _set_if_changed(
                    config, "scope_mcp_server_id",
                    update_data.scope_mcp_server_id, changes,
                )
            if "scope_tool_name" in update_payload_dict:
                new_tool = (
                    update_data.scope_tool_name.strip()
                    if update_data.scope_tool_name else None
                )
                _set_if_changed(
                    config, "scope_tool_name", new_tool, changes,
                )

            # ── Group associations ─────────────────────────────────────
            if update_data.group_ids is not None:
                old_ids = set(config.group_ids)
                new_ids = set(update_data.group_ids)
                if old_ids != new_ids:
                    groups = db.query(ResourceGroup).filter(
                        ResourceGroup.id.in_(update_data.group_ids),
                        ResourceGroup.tenant_id == tenant_id,
                    ).all()
                    config.groups = groups
                    changes["group_ids"] = {
                        "old": list(old_ids), "new": update_data.group_ids
                    }

            # ── Master toggle ──────────────────────────────────────────
            config.enabled = update_data.enabled
            if old_enabled != update_data.enabled:
                changes["enabled"] = {"old": old_enabled, "new": update_data.enabled}

            # ── Streaming output posture ───────────────────────────────
            # None = field omitted; leave the existing value (legacy
            # clients that don't send it don't clobber the operator's
            # choice).
            if update_data.streaming_output_mode:
                _set_if_changed(
                    config,
                    "streaming_output_mode",
                    update_data.streaming_output_mode,
                    changes,
                )

            # ── Surface toggles ────────────────────────────────────────
            # Per-mode LLM + MCP/skill/A2A. Every column tracks
            # one row in the admin UI's Surfaces table; missing any here
            # silently drops the operator's edit on save.
            if update_data.surfaces:
                s = update_data.surfaces
                _set_if_changed(config, "chat_input_enabled", s.chat_input, changes)
                _set_if_changed(config, "chat_output_enabled", s.chat_output, changes)
                _set_if_changed(config, "embeddings_input_enabled", s.embeddings_input, changes)
                _set_if_changed(config, "image_gen_input_enabled", s.image_gen_input, changes)
                _set_if_changed(config, "image_gen_output_enabled", s.image_gen_output, changes)
                _set_if_changed(config, "audio_tts_input_enabled", s.audio_tts_input, changes)
                _set_if_changed(config, "audio_tts_output_enabled", s.audio_tts_output, changes)
                _set_if_changed(config, "audio_stt_input_enabled", s.audio_stt_input, changes)
                _set_if_changed(config, "audio_stt_output_enabled", s.audio_stt_output, changes)
                _set_if_changed(config, "video_gen_input_enabled", s.video_gen_input, changes)
                _set_if_changed(config, "video_gen_output_enabled", s.video_gen_output, changes)
                _set_if_changed(config, "batch_input_enabled", s.batch_input, changes)
                _set_if_changed(config, "batch_output_enabled", s.batch_output, changes)
                _set_if_changed(config, "moderation_input_enabled", s.moderation_input, changes)
                _set_if_changed(config, "mcp_input_enabled", s.mcp_input, changes)
                _set_if_changed(config, "mcp_output_enabled", s.mcp_output, changes)
                _set_if_changed(config, "mcp_registration_enabled", s.mcp_registration, changes)
                _set_if_changed(config, "skill_input_enabled", s.skill_input, changes)
                _set_if_changed(config, "skill_output_enabled", s.skill_output, changes)
                _set_if_changed(config, "a2a_inbound_enabled", s.a2a_inbound, changes)
                _set_if_changed(config, "a2a_outbound_enabled", s.a2a_outbound, changes)

            # ── Built-in checks ────────────────────────────────────────
            if update_data.builtin:
                b = update_data.builtin
                _set_if_changed(config, "pii_detection_enabled", b.pii_enabled, changes)
                _set_if_changed(config, "pii_action", b.pii_action, changes)
                _set_if_changed(config, "pii_provider", b.pii_provider, changes)
                _set_if_changed(config, "pii_categories", b.pii_categories, changes)
                # Only write threshold when the caller explicitly
                # set it (model_fields_set), so legacy clients that omit
                # the field don't clobber an existing value.
                if "pii_threshold" in getattr(b, "model_fields_set", set()):
                    _set_if_changed(config, "pii_threshold", b.pii_threshold, changes)
                _set_if_changed(config, "prompt_injection_enabled", b.prompt_injection_enabled, changes)
                _set_if_changed(config, "prompt_injection_action", b.prompt_injection_action, changes)
                _set_if_changed(config, "prompt_injection_sensitivity", b.prompt_injection_sensitivity, changes)
                _set_if_changed(config, "prompt_injection_provider", b.prompt_injection_provider, changes)
                _set_if_changed(config, "jailbreak_detection_enabled", b.jailbreak_enabled, changes)
                _set_if_changed(config, "jailbreak_action", b.jailbreak_action, changes)
                _set_if_changed(config, "jailbreak_provider", b.jailbreak_provider, changes)
                _set_if_changed(config, "toxic_content_enabled", b.toxic_content_enabled, changes)
                _set_if_changed(config, "toxic_content_action", b.toxic_content_action, changes)
                _set_if_changed(config, "toxic_content_threshold", b.toxic_content_threshold, changes)
                _set_if_changed(config, "toxic_content_provider", b.toxic_content_provider, changes)
                _set_if_changed(config, "toxic_content_category_thresholds", b.toxic_content_category_thresholds, changes)
                _set_if_changed(config, "banned_words", b.banned_words, changes)
                _set_if_changed(config, "banned_patterns", b.banned_patterns, changes)
                _set_if_changed(config, "banned_words_match_mode", b.banned_words_match_mode, changes)
                # Per-check surfaces. Null means
                # "leave the row unchanged" so PATCH semantics still
                # work the same way the rest of this block uses
                # _set_if_changed (null skips, value writes).
                if b.pii_surfaces is not None:
                    _set_if_changed(config, "pii_surfaces", b.pii_surfaces, changes)
                if b.prompt_injection_surfaces is not None:
                    _set_if_changed(config, "prompt_injection_surfaces", b.prompt_injection_surfaces, changes)
                if b.jailbreak_surfaces is not None:
                    _set_if_changed(config, "jailbreak_surfaces", b.jailbreak_surfaces, changes)
                if b.toxic_content_surfaces is not None:
                    _set_if_changed(config, "toxic_content_surfaces", b.toxic_content_surfaces, changes)
                if b.banned_words_surfaces is not None:
                    _set_if_changed(config, "banned_words_surfaces", b.banned_words_surfaces, changes)
                if b.banned_patterns_surfaces is not None:
                    _set_if_changed(config, "banned_patterns_surfaces", b.banned_patterns_surfaces, changes)
                # Secrets detection.
                _set_if_changed(config, "secrets_detection_enabled", b.secrets_enabled, changes)
                _set_if_changed(config, "secrets_action", b.secrets_action, changes)
                if b.secrets_categories is not None:
                    _set_if_changed(config, "secrets_categories", b.secrets_categories, changes)
                if b.secrets_surfaces is not None:
                    _set_if_changed(config, "secrets_surfaces", b.secrets_surfaces, changes)
                _set_if_changed(config, "secrets_threshold", b.secrets_threshold, changes)
                # Per-surface overrides. Distinguish absent
                # (legacy client / partial PATCH) from explicit-null
                # (operator removed the last card) via
                # model_fields_set so we never clobber an existing
                # column when the caller didn't intend to touch it.
                _set_per_surface_if_present(b, config, changes)

            # ── External providers ─────────────────────────────────────
            if update_data.providers is not None:
                new_providers = [p.model_dump() for p in update_data.providers]
                old_providers = config.providers
                _store_ready_providers(new_providers, old_providers, tenant_id)
                config.providers = new_providers
                if old_providers != new_providers:
                    changes["providers"] = {
                        "old_count": len(old_providers or []),
                        "new_count": len(new_providers),
                    }

            # ── Performance settings ───────────────────────────────────
            _set_if_changed(config, "max_check_timeout_ms", update_data.max_check_timeout_ms, changes)
            _set_if_changed(config, "fail_open", update_data.fail_open, changes)
            _set_if_changed(config, "fail_open_overrides", update_data.fail_open_overrides, changes)
            _set_if_changed(config, "log_all_checks", update_data.log_all_checks, changes)

            db.flush()

            logger.info("Guardrail config updated", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "is_new": is_new,
                "enabled": config.enabled,
                "changes": list(changes.keys()),
            })

            # Audit
            audit.log(
                action="guardrail_config_create" if is_new else "guardrail_config_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GUARDRAILS.value,
                resource_id=config.id,
                details={
                    "enabled": config.enabled,
                    "is_new": is_new,
                    "changes": changes,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "guardrail", tenant_id, config.id))

            return GuardrailConfigDetailResponse(
                config=_config_to_response(config)
            )

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to update guardrail config", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to update guardrail configuration"
            )

    # =========================================================================
    # List All Configurations
    # =========================================================================

    @router.get(
        "/configs",
        response_model=GuardrailConfigListResponse,
        summary="List guardrail configurations",
        description="""
        List all guardrail configurations for the current tenant.
        Includes name, description, group associations, and full settings.
        """
    )
    def list_guardrail_configs(
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_READ))
    ):
        """List all guardrail configs for the tenant"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            configs = db.query(GuardrailConfig).filter(
                GuardrailConfig.tenant_id == tenant_id
            ).order_by(
                GuardrailConfig.is_default.desc(),
                GuardrailConfig.priority.desc(),
                GuardrailConfig.name,
            ).all()

            logger.debug("Guardrail configs listed", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "count": len(configs),
            })

            return GuardrailConfigListResponse(
                configs=[_config_to_response(c) for c in configs],
                total=len(configs),
            )

        except Exception as e:
            logger.error("Failed to list guardrail configs", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to list guardrail configurations"
            )

    # =========================================================================
    # Create Configuration
    # =========================================================================

    # ======================================================================
    # Break-glass
    # ======================================================================
    #
    # The escape hatch at the top of the guardrail degradation ladder. When a
    # detector provider is genuinely down and availability outranks the
    # control, an operator declares it here and checks degrade to
    # log-and-allow for the duration.
    #
    # This produces the same outcome a hardcoded fail-open would. The reason
    # it is a record instead of a code default: a name is attached, a reason
    # is required, and it expires on its own. That restores the cost of the
    # decision, which is the only thing that ever stops fail-open becoming
    # the permanent default.

    def _live_break_glass(db: Session, tenant_id: str):
        """The in-force window, if any.

        Expiry is evaluated here rather than by a sweeper: a background job
        that fails to run must never be the reason a tenant stays unguarded.
        """
        rows = (
            db.query(GuardrailBreakGlass)
            .filter(
                GuardrailBreakGlass.tenant_id == tenant_id,
                GuardrailBreakGlass.deactivated_at.is_(None),
            )
            .order_by(GuardrailBreakGlass.activated_at.desc())
            .limit(20)
            .all()
        )
        return next((r for r in rows if r.is_active()), None)

    @router.get(
        "/break-glass",
        summary="Current guardrail break-glass state",
        description=(
            "Whether checks are currently degraded to log-and-allow for this "
            "tenant, who declared it, why, and when it expires."
        ),
    )
    def get_break_glass(
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)
        ),
        db: Session = Depends(get_db),
        _perm=Depends(require_permission(Permission.GUARDRAIL_READ)),
    ):
        _admin, tenant_id, _t = admin_and_tenant
        live = _live_break_glass(db, tenant_id)
        history = (
            db.query(GuardrailBreakGlass)
            .filter(GuardrailBreakGlass.tenant_id == tenant_id)
            .order_by(GuardrailBreakGlass.activated_at.desc())
            .limit(10)
            .all()
        )
        return {
            "active": live is not None,
            "current": live.to_dict() if live else None,
            "history": [h.to_dict() for h in history],
            "max_hours": MAX_BREAK_GLASS_HOURS,
            "default_minutes": DEFAULT_BREAK_GLASS_MINUTES,
        }

    @router.post(
        "/break-glass",
        status_code=201,
        summary="Declare a guardrail outage (break-glass)",
        description=(
            "Degrade guardrail checks to log-and-allow for a bounded window. "
            "Requires a reason and always expires — capped at "
            f"{MAX_BREAK_GLASS_HOURS} hours, because a break-glass that "
            "outlives its incident is a policy change nobody reviewed."
        ),
    )
    def activate_break_glass(
        payload: BreakGlassActivateRequest,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.GUARDRAIL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        _admin, tenant_id, _t = admin_and_tenant

        existing = _live_break_glass(db, tenant_id)
        if existing:
            # Renewing is an explicit deactivate-then-activate, so the log
            # shows two decisions rather than one that silently grew.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Break-glass is already active until "
                    f"{existing.expires_at.isoformat()}. Deactivate it first "
                    "to change the window."
                ),
            )

        minutes = GuardrailBreakGlass.clamp_duration(payload.duration_minutes)
        row = GuardrailBreakGlass(
            id=f"gbg-{ULID()}",
            tenant_id=tenant_id,
            reason=payload.reason.strip(),
            activated_by=getattr(admin, "username", None) or "unknown",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        logger.warning(
            f"Guardrail break-glass ACTIVATED for tenant {tenant_id} by "
            f"{row.activated_by} until {row.expires_at}: {row.reason}",
            {
                "tenant_id": tenant_id,
                "activated_by": row.activated_by,
                "expires_at": str(row.expires_at),
            },
        )
        audit.log(
            db=db,
            action="guardrail_break_glass_activate",
            admin=admin,
            tenant_id=tenant_id,
            success=True,
            resource_type="guardrail_break_glass",
            resource_id=row.id,
            details={
                "reason": row.reason,
                "expires_at": row.expires_at.isoformat(),
                "duration_minutes": minutes,
                "clamped": bool(
                    payload.duration_minutes and payload.duration_minutes > minutes
                ),
            },
            request_path=str(request.url.path),
        )
        return row.to_dict()

    @router.delete(
        "/break-glass",
        summary="End the break-glass window early",
        description="Restores normal guardrail enforcement immediately.",
    )
    def deactivate_break_glass(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.GUARDRAIL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        _admin, tenant_id, _t = admin_and_tenant
        live = _live_break_glass(db, tenant_id)
        if not live:
            # Idempotent: "enforcement is on" is the desired end state either
            # way, and erroring mid-incident helps nobody.
            return {"active": False, "current": None}

        live.deactivated_at = datetime.now(timezone.utc)
        live.deactivated_by = getattr(admin, "username", None) or "unknown"
        db.commit()
        db.refresh(live)

        logger.warning(
            f"Guardrail break-glass DEACTIVATED for tenant {tenant_id} by "
            f"{live.deactivated_by}",
            {"tenant_id": tenant_id, "deactivated_by": live.deactivated_by},
        )
        audit.log(
            db=db,
            action="guardrail_break_glass_deactivate",
            admin=admin,
            tenant_id=tenant_id,
            success=True,
            resource_type="guardrail_break_glass",
            resource_id=live.id,
            details={"reason": live.reason},
            request_path=str(request.url.path),
        )
        return {"active": False, "current": live.to_dict()}

    @router.post(
        "/configs",
        response_model=GuardrailConfigDetailResponse,
        status_code=201,
        summary="Create guardrail configuration",
        description="""
        Create a new guardrail configuration for the current tenant.
        Each config can be associated with resource groups.
        """
    )
    def create_guardrail_config(
            create_data: GuardrailConfigCreateRequest,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create a new guardrail configuration"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        # Feature gate.
        TenantService.check_feature_access(db, tenant_id, 'guardrails')

        # Provider-completeness gate.
        _validate_provider_completeness(create_data.builtin, create_data.providers)

        try:
            # Check name uniqueness
            existing = db.query(GuardrailConfig).filter(
                GuardrailConfig.tenant_id == tenant_id,
                GuardrailConfig.name == create_data.name,
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"A guardrail config named '{create_data.name}' already exists"
                )

            # If setting as default, clear other defaults
            if create_data.is_default:
                db.query(GuardrailConfig).filter(
                    GuardrailConfig.tenant_id == tenant_id,
                    GuardrailConfig.is_default == True,
                ).update({"is_default": False})

            config = GuardrailConfig(
                id=str(ULID()),
                tenant_id=tenant_id,
                name=create_data.name,
                description=create_data.description,
                is_default=create_data.is_default,
                priority=create_data.priority,
                # Per-call scope filters (default NULL =
                # applies to all calls).
                scope_mcp_server_id=create_data.scope_mcp_server_id,
                scope_tool_name=(
                    create_data.scope_tool_name.strip()
                    if create_data.scope_tool_name else None
                ),
                enabled=create_data.enabled,
                # Streaming output guardrail posture.
                streaming_output_mode=(create_data.streaming_output_mode or "auto"),
                max_check_timeout_ms=create_data.max_check_timeout_ms,
                fail_open=create_data.fail_open,
                fail_open_overrides=create_data.fail_open_overrides,
                log_all_checks=create_data.log_all_checks,
            )

            # Apply surface toggles
            if create_data.surfaces:
                s = create_data.surfaces
                # Per-mode LLM surfaces. All 14 columns track the
                # admin UI's Surfaces table 1:1.
                config.chat_input_enabled = s.chat_input
                config.chat_output_enabled = s.chat_output
                config.embeddings_input_enabled = s.embeddings_input
                config.image_gen_input_enabled = s.image_gen_input
                config.image_gen_output_enabled = s.image_gen_output
                config.audio_tts_input_enabled = s.audio_tts_input
                config.audio_tts_output_enabled = s.audio_tts_output
                config.audio_stt_input_enabled = s.audio_stt_input
                config.audio_stt_output_enabled = s.audio_stt_output
                config.video_gen_input_enabled = s.video_gen_input
                config.video_gen_output_enabled = s.video_gen_output
                config.batch_input_enabled = s.batch_input
                config.batch_output_enabled = s.batch_output
                config.moderation_input_enabled = s.moderation_input
                # MCP / skill / A2A surfaces.
                config.mcp_input_enabled = s.mcp_input
                config.mcp_output_enabled = s.mcp_output
                config.mcp_registration_enabled = s.mcp_registration
                config.skill_input_enabled = s.skill_input
                config.skill_output_enabled = s.skill_output
                config.a2a_inbound_enabled = s.a2a_inbound
                config.a2a_outbound_enabled = s.a2a_outbound

            # Apply builtin checks
            if create_data.builtin:
                b = create_data.builtin
                config.pii_detection_enabled = b.pii_enabled
                config.pii_action = b.pii_action
                config.pii_provider = b.pii_provider
                config.pii_categories = b.pii_categories
                config.pii_threshold = b.pii_threshold
                config.prompt_injection_enabled = b.prompt_injection_enabled
                config.prompt_injection_action = b.prompt_injection_action
                config.prompt_injection_sensitivity = b.prompt_injection_sensitivity
                config.prompt_injection_provider = b.prompt_injection_provider
                config.jailbreak_detection_enabled = b.jailbreak_enabled
                config.jailbreak_action = b.jailbreak_action
                config.jailbreak_provider = b.jailbreak_provider
                config.toxic_content_enabled = b.toxic_content_enabled
                config.toxic_content_action = b.toxic_content_action
                config.toxic_content_threshold = b.toxic_content_threshold
                config.toxic_content_provider = b.toxic_content_provider
                config.toxic_content_category_thresholds = b.toxic_content_category_thresholds
                config.banned_words = b.banned_words
                config.banned_patterns = b.banned_patterns
                config.banned_words_match_mode = b.banned_words_match_mode
                # Per-check surfaces. Null on
                # create means "let the migration default backfill on
                # first read" (DB default is null → to_dict() emits
                # the module-level default constants).
                config.pii_surfaces = b.pii_surfaces
                config.prompt_injection_surfaces = b.prompt_injection_surfaces
                config.jailbreak_surfaces = b.jailbreak_surfaces
                config.toxic_content_surfaces = b.toxic_content_surfaces
                config.banned_words_surfaces = b.banned_words_surfaces
                config.banned_patterns_surfaces = b.banned_patterns_surfaces
                # Secrets detection.
                config.secrets_detection_enabled = b.secrets_enabled
                config.secrets_action = b.secrets_action
                config.secrets_categories = b.secrets_categories
                config.secrets_surfaces = b.secrets_surfaces
                config.secrets_threshold = b.secrets_threshold
                # Per-surface override maps. NULL columns are
                # the no-overrides baseline; the form serializes
                # null for unset and a dict when the operator added
                # cards. Persist as-is on create.
                config.pii_per_surface = b.pii_per_surface
                config.prompt_injection_per_surface = b.prompt_injection_per_surface
                config.jailbreak_per_surface = b.jailbreak_per_surface
                config.toxic_content_per_surface = b.toxic_content_per_surface
                config.secrets_per_surface = b.secrets_per_surface
                config.banned_words_per_surface = b.banned_words_per_surface
                config.banned_patterns_per_surface = b.banned_patterns_per_surface

            # Apply providers
            if create_data.providers:
                new_providers = [p.model_dump() for p in create_data.providers]
                _store_ready_providers(new_providers, None, tenant_id)
                config.providers = new_providers

            db.add(config)

            # Apply group associations
            if create_data.group_ids:
                groups = db.query(ResourceGroup).filter(
                    ResourceGroup.id.in_(create_data.group_ids),
                    ResourceGroup.tenant_id == tenant_id,
                ).all()
                config.groups = groups

            db.flush()

            logger.info("Guardrail config created", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "config_id": config.id,
                "config_name": config.name,
            })

            audit.log(
                action="guardrail_config_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GUARDRAILS.value,
                resource_id=config.id,
                details={"name": config.name, "enabled": config.enabled},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "guardrail", tenant_id, config.id))

            return GuardrailConfigDetailResponse(
                config=_config_to_response(config)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create guardrail config", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to create guardrail configuration"
            )

    # =========================================================================
    # Get Specific Configuration
    # =========================================================================

    @router.get(
        "/configs/{config_id}",
        response_model=GuardrailConfigDetailResponse,
        summary="Get guardrail configuration by ID",
    )
    def get_guardrail_config_by_id(
            config_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_READ))
    ):
        """Get a specific guardrail config by ID"""
        admin, tenant_id, admin_type = admin_and_tenant

        config = db.query(GuardrailConfig).filter(
            GuardrailConfig.id == config_id,
            GuardrailConfig.tenant_id == tenant_id,
        ).first()

        if not config:
            raise HTTPException(status_code=404, detail=f"Guardrail config {config_id} not found")

        return GuardrailConfigDetailResponse(
            config=_config_to_response(config)
        )

    # =========================================================================
    # Update Specific Configuration
    # =========================================================================

    @router.patch(
        "/configs/{config_id}",
        response_model=GuardrailConfigDetailResponse,
        summary="Update guardrail configuration by ID",
    )
    def update_guardrail_config_by_id(
            config_id: str,
            update_data: GuardrailConfigUpdate,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update a specific guardrail config"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        # Provider-completeness gate.
        _validate_provider_completeness(update_data.builtin, update_data.providers)

        try:
            config = db.query(GuardrailConfig).filter(
                GuardrailConfig.id == config_id,
                GuardrailConfig.tenant_id == tenant_id,
            ).first()

            if not config:
                raise HTTPException(status_code=404, detail=f"Guardrail config {config_id} not found")

            # Check name uniqueness if changing
            if update_data.name is not None and update_data.name != config.name:
                dup = db.query(GuardrailConfig).filter(
                    GuardrailConfig.tenant_id == tenant_id,
                    GuardrailConfig.name == update_data.name,
                    GuardrailConfig.id != config_id,
                ).first()
                if dup:
                    raise HTTPException(
                        status_code=409,
                        detail=f"A guardrail config named '{update_data.name}' already exists"
                    )

            changes = {}
            old_enabled = config.enabled

            # ── Identity fields ────────────────────────────────────────
            if update_data.name is not None:
                _set_if_changed(config, "name", update_data.name, changes)
            if update_data.description is not None:
                _set_if_changed(config, "description", update_data.description, changes)
            if update_data.is_default is not None:
                if update_data.is_default and not config.is_default:
                    db.query(GuardrailConfig).filter(
                        GuardrailConfig.tenant_id == tenant_id,
                        GuardrailConfig.id != config.id,
                        GuardrailConfig.is_default == True,
                    ).update({"is_default": False})
                _set_if_changed(config, "is_default", update_data.is_default, changes)
            if update_data.priority is not None:
                _set_if_changed(config, "priority", update_data.priority, changes)

            # Per-call scope filters. PATCH semantics:
            # explicit `null` clears the scope; absent field is
            # left untouched. We use model_dump(exclude_unset=True)
            # to detect deliberate presence-of-key vs absence.
            update_payload_dict = update_data.model_dump(exclude_unset=True)
            if "scope_mcp_server_id" in update_payload_dict:
                _set_if_changed(
                    config, "scope_mcp_server_id",
                    update_data.scope_mcp_server_id, changes,
                )
            if "scope_tool_name" in update_payload_dict:
                new_tool = (
                    update_data.scope_tool_name.strip()
                    if update_data.scope_tool_name else None
                )
                _set_if_changed(
                    config, "scope_tool_name", new_tool, changes,
                )

            # ── Group associations ─────────────────────────────────────
            if update_data.group_ids is not None:
                old_ids = set(config.group_ids)
                new_ids = set(update_data.group_ids)
                if old_ids != new_ids:
                    groups = db.query(ResourceGroup).filter(
                        ResourceGroup.id.in_(update_data.group_ids),
                        ResourceGroup.tenant_id == tenant_id,
                    ).all()
                    config.groups = groups
                    changes["group_ids"] = {"old": list(old_ids), "new": update_data.group_ids}

            # ── Master toggle ──────────────────────────────────────────
            config.enabled = update_data.enabled
            if old_enabled != update_data.enabled:
                changes["enabled"] = {"old": old_enabled, "new": update_data.enabled}

            # ── Streaming output posture ───────────────────────────────
            # None = field omitted; leave the existing value (legacy
            # clients that don't send it don't clobber the operator's
            # choice).
            if update_data.streaming_output_mode:
                _set_if_changed(
                    config,
                    "streaming_output_mode",
                    update_data.streaming_output_mode,
                    changes,
                )

            # ── Surface toggles ────────────────────────────────────────
            # Per-mode LLM + MCP/skill/A2A. Every column tracks
            # one row in the admin UI's Surfaces table; missing any here
            # silently drops the operator's edit on save.
            if update_data.surfaces:
                s = update_data.surfaces
                _set_if_changed(config, "chat_input_enabled", s.chat_input, changes)
                _set_if_changed(config, "chat_output_enabled", s.chat_output, changes)
                _set_if_changed(config, "embeddings_input_enabled", s.embeddings_input, changes)
                _set_if_changed(config, "image_gen_input_enabled", s.image_gen_input, changes)
                _set_if_changed(config, "image_gen_output_enabled", s.image_gen_output, changes)
                _set_if_changed(config, "audio_tts_input_enabled", s.audio_tts_input, changes)
                _set_if_changed(config, "audio_tts_output_enabled", s.audio_tts_output, changes)
                _set_if_changed(config, "audio_stt_input_enabled", s.audio_stt_input, changes)
                _set_if_changed(config, "audio_stt_output_enabled", s.audio_stt_output, changes)
                _set_if_changed(config, "video_gen_input_enabled", s.video_gen_input, changes)
                _set_if_changed(config, "video_gen_output_enabled", s.video_gen_output, changes)
                _set_if_changed(config, "batch_input_enabled", s.batch_input, changes)
                _set_if_changed(config, "batch_output_enabled", s.batch_output, changes)
                _set_if_changed(config, "moderation_input_enabled", s.moderation_input, changes)
                _set_if_changed(config, "mcp_input_enabled", s.mcp_input, changes)
                _set_if_changed(config, "mcp_output_enabled", s.mcp_output, changes)
                _set_if_changed(config, "mcp_registration_enabled", s.mcp_registration, changes)
                _set_if_changed(config, "skill_input_enabled", s.skill_input, changes)
                _set_if_changed(config, "skill_output_enabled", s.skill_output, changes)
                _set_if_changed(config, "a2a_inbound_enabled", s.a2a_inbound, changes)
                _set_if_changed(config, "a2a_outbound_enabled", s.a2a_outbound, changes)

            # ── Built-in checks ────────────────────────────────────────
            if update_data.builtin:
                b = update_data.builtin
                _set_if_changed(config, "pii_detection_enabled", b.pii_enabled, changes)
                _set_if_changed(config, "pii_action", b.pii_action, changes)
                _set_if_changed(config, "pii_provider", b.pii_provider, changes)
                _set_if_changed(config, "pii_categories", b.pii_categories, changes)
                # Only write threshold when the caller explicitly
                # set it (model_fields_set), so legacy clients that omit
                # the field don't clobber an existing value.
                if "pii_threshold" in getattr(b, "model_fields_set", set()):
                    _set_if_changed(config, "pii_threshold", b.pii_threshold, changes)
                _set_if_changed(config, "prompt_injection_enabled", b.prompt_injection_enabled, changes)
                _set_if_changed(config, "prompt_injection_action", b.prompt_injection_action, changes)
                _set_if_changed(config, "prompt_injection_sensitivity", b.prompt_injection_sensitivity, changes)
                _set_if_changed(config, "prompt_injection_provider", b.prompt_injection_provider, changes)
                _set_if_changed(config, "jailbreak_detection_enabled", b.jailbreak_enabled, changes)
                _set_if_changed(config, "jailbreak_action", b.jailbreak_action, changes)
                _set_if_changed(config, "jailbreak_provider", b.jailbreak_provider, changes)
                _set_if_changed(config, "toxic_content_enabled", b.toxic_content_enabled, changes)
                _set_if_changed(config, "toxic_content_action", b.toxic_content_action, changes)
                _set_if_changed(config, "toxic_content_threshold", b.toxic_content_threshold, changes)
                _set_if_changed(config, "toxic_content_provider", b.toxic_content_provider, changes)
                _set_if_changed(config, "toxic_content_category_thresholds", b.toxic_content_category_thresholds, changes)
                _set_if_changed(config, "banned_words", b.banned_words, changes)
                _set_if_changed(config, "banned_patterns", b.banned_patterns, changes)
                _set_if_changed(config, "banned_words_match_mode", b.banned_words_match_mode, changes)
                # Per-check surfaces. Null means
                # "leave the row unchanged" so PATCH semantics still
                # work the same way the rest of this block uses
                # _set_if_changed (null skips, value writes).
                if b.pii_surfaces is not None:
                    _set_if_changed(config, "pii_surfaces", b.pii_surfaces, changes)
                if b.prompt_injection_surfaces is not None:
                    _set_if_changed(config, "prompt_injection_surfaces", b.prompt_injection_surfaces, changes)
                if b.jailbreak_surfaces is not None:
                    _set_if_changed(config, "jailbreak_surfaces", b.jailbreak_surfaces, changes)
                if b.toxic_content_surfaces is not None:
                    _set_if_changed(config, "toxic_content_surfaces", b.toxic_content_surfaces, changes)
                if b.banned_words_surfaces is not None:
                    _set_if_changed(config, "banned_words_surfaces", b.banned_words_surfaces, changes)
                if b.banned_patterns_surfaces is not None:
                    _set_if_changed(config, "banned_patterns_surfaces", b.banned_patterns_surfaces, changes)
                # Secrets detection.
                _set_if_changed(config, "secrets_detection_enabled", b.secrets_enabled, changes)
                _set_if_changed(config, "secrets_action", b.secrets_action, changes)
                if b.secrets_categories is not None:
                    _set_if_changed(config, "secrets_categories", b.secrets_categories, changes)
                if b.secrets_surfaces is not None:
                    _set_if_changed(config, "secrets_surfaces", b.secrets_surfaces, changes)
                _set_if_changed(config, "secrets_threshold", b.secrets_threshold, changes)
                # Per-surface overrides (see PATCH endpoint above).
                _set_per_surface_if_present(b, config, changes)

            # ── External providers ─────────────────────────────────────
            if update_data.providers is not None:
                new_providers = [p.model_dump() for p in update_data.providers]
                old_providers = config.providers
                _store_ready_providers(new_providers, old_providers, tenant_id)
                config.providers = new_providers
                if old_providers != new_providers:
                    changes["providers"] = {
                        "old_count": len(old_providers or []),
                        "new_count": len(new_providers),
                    }

            # ── Performance settings ───────────────────────────────────
            _set_if_changed(config, "max_check_timeout_ms", update_data.max_check_timeout_ms, changes)
            _set_if_changed(config, "fail_open", update_data.fail_open, changes)
            _set_if_changed(config, "fail_open_overrides", update_data.fail_open_overrides, changes)
            _set_if_changed(config, "log_all_checks", update_data.log_all_checks, changes)

            db.flush()
            audit.log(
                action="guardrail_config_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GUARDRAILS.value,
                resource_id=config.id,
                details={"changes": list(changes.keys())},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "guardrail", tenant_id, config_id))

            return GuardrailConfigDetailResponse(
                config=_config_to_response(config)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update guardrail config", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to update guardrail configuration"
            )

    # =========================================================================
    # Delete Configuration
    # =========================================================================

    @router.delete(
        "/configs/{config_id}",
        status_code=200,
        summary="Delete guardrail configuration",
    )
    def delete_guardrail_config(
            config_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete a guardrail configuration"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            config = db.query(GuardrailConfig).filter(
                GuardrailConfig.id == config_id,
                GuardrailConfig.tenant_id == tenant_id,
            ).first()

            if not config:
                raise HTTPException(status_code=404, detail=f"Guardrail config {config_id} not found")

            config_name = config.name

            # Clear group associations before delete
            config.groups = []
            db.delete(config)
            db.flush()

            logger.info("Guardrail config deleted", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "config_id": config_id,
                "config_name": config_name,
            })

            audit.log(
                action="guardrail_config_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GUARDRAILS.value,
                resource_id=config_id,
                details={"name": config_name},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "guardrail", tenant_id, config_id))

            return {"message": f"Guardrail config '{config_name}' deleted", "id": config_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete guardrail config", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to delete guardrail configuration"
            )

    # =========================================================================
    # Set Group Associations
    # =========================================================================

    @router.patch(
        "/configs/{config_id}/groups",
        response_model=GuardrailConfigDetailResponse,
        summary="Set resource group associations",
    )
    def set_guardrail_groups(
            config_id: str,
            group_data: GuardrailGroupAssociationRequest,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Set resource group associations for a guardrail config"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            config = db.query(GuardrailConfig).filter(
                GuardrailConfig.id == config_id,
                GuardrailConfig.tenant_id == tenant_id,
            ).first()

            if not config:
                raise HTTPException(status_code=404, detail=f"Guardrail config {config_id} not found")

            old_ids = config.group_ids

            groups = db.query(ResourceGroup).filter(
                ResourceGroup.id.in_(group_data.group_ids),
                ResourceGroup.tenant_id == tenant_id,
            ).all() if group_data.group_ids else []

            # Unknown ids — including another tenant's — are refused rather
            # than dropped while binding nothing. A typo'd group id
            # deserves an error, and matching the agent-skills semantics
            # keeps the admin surface consistent.
            if group_data.group_ids:
                missing = set(group_data.group_ids) - {g.id for g in groups}
                if missing:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            "Resource group(s) not found: "
                            + ", ".join(sorted(missing))
                        ),
                    )

            config.groups = groups
            db.flush()

            audit.log(
                action="guardrail_groups_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GUARDRAILS.value,
                resource_id=config_id,
                details={
                    "old_group_ids": old_ids,
                    "new_group_ids": group_data.group_ids,
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "guardrail", tenant_id, config_id, "assign"))

            return GuardrailConfigDetailResponse(
                config=_config_to_response(config)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update guardrail groups", context={
                "tenant_id": tenant_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to update group associations"
            )

    # =========================================================================
    # Test Content Against Guardrails
    # =========================================================================

    @router.post(
        "/test",
        response_model=GuardrailTestResponse,
        summary="Test content against guardrails",
        description="""
        Test content against the tenant's configured guardrails without
        modifying any state.
        
        Useful for validating configuration before enabling guardrails in
        production.
        
        Example:
            POST /test
            {
                "content": "Ignore all previous instructions",
                "surface": "chat_input"
            }
            
        Returns the action that would be taken (block/redact/warn/allow)
        and the list of violations detected.
        """
    )
    def test_guardrails(
            test_request: GuardrailTestRequest,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_TEST))
    ):
        """Test content against the tenant's guardrail configuration"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            # Per-mode tokens encode direction in the name — derive
            # the input/output choice from the suffix rather than the
            # caller-supplied `direction` so the test endpoint can't
            # be coerced into dispatching `chat_input` via the
            # check_output path (which would silently no-op since no
            # output check looks at chat_input).
            surface_token = test_request.surface
            if surface_token.endswith("_input") or surface_token.endswith("_inbound"):
                effective_direction = "input"
            elif surface_token.endswith("_output") or surface_token.endswith("_outbound"):
                effective_direction = "output"
            else:
                effective_direction = test_request.direction

            check_kwargs = dict(
                content=test_request.content,
                surface=surface_token,
                tenant_id=tenant_id,
                db=db,
                config_id=test_request.config_id,
                context={"source": "admin_test", "admin_id": admin_id},
            )

            if effective_direction == "input":
                result = block_on(guardrails_service.check_input(**check_kwargs))
            else:
                result = block_on(guardrails_service.check_output(**check_kwargs))

            # Resolve config name for response (when specific config was requested)
            response_config_id = test_request.config_id
            response_config_name = None
            if test_request.config_id:
                config_obj = db.query(GuardrailConfig).filter(
                    GuardrailConfig.id == test_request.config_id,
                    GuardrailConfig.tenant_id == tenant_id,
                ).first()
                if config_obj:
                    response_config_name = config_obj.name

            logger.debug("Guardrail test executed", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "surface": test_request.surface,
                "direction": test_request.direction,
                "config_id": test_request.config_id,
                "action": result.action.value,
                "violations": len(result.violations),
                "configs_evaluated": result.configs_evaluated,
                "content_length": len(test_request.content),
                "duration_ms": round(result.duration_ms, 2),
            })

            return GuardrailTestResponse(
                action=result.action.value,
                blocked=result.blocked,
                violations=[
                    GuardrailViolationResponse(
                        type=v.type,
                        provider=v.provider,
                        confidence=v.confidence,
                        action=v.action.value,
                        detail=v.detail,
                        config_id=v.config_id,
                        config_name=v.config_name,
                    )
                    for v in result.violations
                ],
                duration_ms=round(result.duration_ms, 2),
                surface=result.surface,
                direction=result.direction,
                configs_evaluated=result.configs_evaluated,
                config_id=response_config_id,
                config_name=response_config_name,
            )

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to test guardrails", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to test guardrails"
            )

    # =========================================================================
    # Test Provider Connection (reachability + auth probe)
    # =========================================================================

    @router.post(
        "/providers/test",
        response_model=GuardrailProviderTestResponse,
        summary="Test an external guardrail provider connection",
        description="""
        Probe an external guardrail provider for **reachability + auth**,
        distinct from POST /test (which tests detection *behaviour*).

        Guardrail configs run `fail_open: false` by default, so a
        misconfigured provider endpoint (wrong host, bad key, unreachable
        analyzer) now surfaces as refused traffic rather than silently
        passing. That is the safe direction, but a noisy one to discover in
        production — and any config the operator has deliberately set to
        `fail_open: true` still fails silently. This endpoint distinguishes
        "reachable + authenticated" from "will not work when it matters",
        before either failure mode reaches live traffic.

        Send the provider config as entered in the form. Because `api_key`
        is excluded from the config GET response, testing an already-saved
        provider has no key to send — so when `api_key` is omitted and
        `config_id` is supplied, the saved key/endpoint for that
        (config, provider) is used.
        """
    )
    def test_provider_connection(
            test_request: GuardrailProviderTestRequest,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_TEST))
    ):
        """Probe an external guardrail provider for reachability + auth."""
        from app.services.guardrails.provider_probe import probe_provider
        import time as _time

        admin_user, tenant_id, admin_type = admin_and_tenant

        provider = test_request.provider
        endpoint = test_request.endpoint
        api_key = test_request.api_key
        config = test_request.config

        # Recover saved secrets/endpoint when the form omitted them
        # (api_key is never returned to the client, so an unchanged
        # provider arrives here without its key). Only the matching
        # tenant's own config is consulted — tenant isolation is enforced
        # by require_tenant_resource_access above + the tenant_id filter.
        if test_request.config_id:
            saved = (
                db.query(GuardrailConfig)
                .filter(
                    GuardrailConfig.id == test_request.config_id,
                    GuardrailConfig.tenant_id == tenant_id,
                )
                .first()
            )
            if saved and saved.providers:
                for entry in saved.providers:
                    if (entry or {}).get("provider", "").lower() == provider:
                        if not api_key:
                            saved_key = entry.get("api_key")
                            # Stored encrypted at rest — decrypt for the probe.
                            if saved_key and encryption_service is not None:
                                try:
                                    saved_key = encryption_service.decrypt(saved_key)
                                except Exception:
                                    # Leave as-is if it wasn't encrypted (e.g.
                                    # written by a path that didn't encrypt).
                                    pass
                            api_key = saved_key
                        if not endpoint:
                            endpoint = entry.get("endpoint")
                        # The secrets of `config` are never returned
                        # either: the form's config comes without them.
                        config = provider_secrets.with_stored_secrets(config, entry)
                        break
        # Stored encrypted at rest: in clear for the probe (a value the
        # form carried, or a row from before, is left as it is).
        if encryption_service is not None:
            config = provider_secrets.decrypt_config_secrets(config, encryption_service.decrypt)

        start = _time.perf_counter()
        result = block_on(probe_provider(provider, endpoint, api_key, config))
        elapsed_ms = (_time.perf_counter() - start) * 1000.0

        logger.info("Guardrail provider connection test", context={
            "tenant_id": tenant_id,
            "admin_id": admin_user.id,
            "provider": provider,
            "status": result.status,
            "response_time_ms": round(elapsed_ms, 1),
        })

        return GuardrailProviderTestResponse(
            provider=provider,
            status=result.status,
            ok=result.status == "healthy",
            response_time_ms=round(elapsed_ms, 1),
            detail=result.detail,
            error_message=result.error,
        )

    # =========================================================================
    # List Available Providers
    # =========================================================================

    @router.get(
        "/providers",
        response_model=GuardrailProviderListResponse,
        summary="List available guardrail providers",
        description="""
        List all available guardrail providers and their capabilities.
        
        Use this to build the provider configuration UI in the Admin console.
        Each provider includes its supported modes and whether an API key
        is required.
        """
    )
    async def list_providers(
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_READ))
    ):
        """List available guardrail providers"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            return GuardrailProviderListResponse(providers=[
                GuardrailProviderInfo(
                    id="builtin",
                    name="Built-in Content Filter",
                    description=(
                        "Regex-based PII, prompt injection, jailbreak, and toxic content "
                        "detection. No external dependencies. Sub-millisecond latency."
                    ),
                    modes=["pre_call", "post_call"],
                    requires_api_key=False,
                ),
                GuardrailProviderInfo(
                    id="lakera",
                    name="Lakera Guard",
                    description=(
                        "ML-based prompt injection and jailbreak detection. "
                        "Recommended for production use."
                    ),
                    modes=["pre_call"],
                    requires_api_key=True,
                ),
                GuardrailProviderInfo(
                    id="openai_moderation",
                    name="OpenAI Moderation",
                    description=(
                        "Free content moderation API. Good for toxic/harmful content "
                        "detection across multiple categories."
                    ),
                    modes=["pre_call", "post_call"],
                    requires_api_key=True,
                ),
                GuardrailProviderInfo(
                    id="presidio",
                    name="Microsoft Presidio",
                    description=(
                        "PII detection and anonymisation. Can run locally as a "
                        "container or via hosted endpoint."
                    ),
                    modes=["pre_call", "post_call"],
                    requires_api_key=False,
                ),
                GuardrailProviderInfo(
                    id="bedrock",
                    name="AWS Bedrock Guardrails",
                    description=(
                        "AWS-managed guardrails. Requires AWS credentials "
                        "configured on the gateway host."
                    ),
                    modes=["pre_call", "post_call"],
                    requires_api_key=False,
                ),
                GuardrailProviderInfo(
                    id="custom",
                    name="Custom Webhook",
                    description=(
                        "Your own guardrails endpoint. Receives POST with content, "
                        "returns violations list."
                    ),
                    modes=["pre_call", "post_call"],
                    requires_api_key=False,
                ),
            ])

        except Exception as e:
            logger.error("Failed to list guardrail providers", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to list guardrail providers"
            )

    # =========================================================================
    # Apply audit-recommended per-mode surface defaults
    # =========================================================================

    @router.post(
        "/{config_id}/apply-recommended-mode-defaults",
        summary="Apply audit-recommended per-mode surface defaults",
        description="""
        One-click action that narrows jailbreak / prompt-injection /
        toxic-content checks away from operator-controlled corpora
        (embeddings, batch) and the safety-classifier-bound surface
        (moderation_input), and widens PII / secrets to all input +
        output surfaces.

        Behaviour-preserving for new tenants (the seed already ships
        this posture). Existing tenants must opt in explicitly via
        this endpoint, which is why the recommended narrowing is gated
        on operator consent rather than applied silently.

        Returns the delta so the admin UI can render a confirmation
        modal listing every surface added or removed.
        """,
    )
    def apply_recommended_mode_defaults(
            config_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GUARDRAILS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.GUARDRAIL_UPDATE)),
            audit_logger: UnifiedAdminAuditLogger = Depends(get_audit_logger),
            dry_run: bool = Query(False, description="If true, returns the delta without persisting changes."),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        config = db.query(GuardrailConfig).filter(
            GuardrailConfig.id == config_id,
            GuardrailConfig.tenant_id == tenant_id,
        ).first()
        if not config:
            raise HTTPException(status_code=404, detail="Guardrail configuration not found")

        target_posture = {
            "jailbreak_surfaces": list(AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES),
            "prompt_injection_surfaces": list(AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES),
            "toxic_content_surfaces": list(AUDIT_RECOMMENDED_TOXIC_CONTENT_SURFACES),
            "pii_surfaces": list(AUDIT_RECOMMENDED_WIDE_SURFACES),
            "secrets_surfaces": list(AUDIT_RECOMMENDED_WIDE_SURFACES),
        }

        # Build the delta so the UI can render a confirmation modal
        # listing exactly which surfaces are about to flip on/off per
        # check. Sorted sets so the diff is deterministic on retry.
        delta: dict = {}
        for column, recommended in target_posture.items():
            current = list(getattr(config, column) or [])
            current_set = set(current)
            recommended_set = set(recommended)
            removed = sorted(current_set - recommended_set)
            added = sorted(recommended_set - current_set)
            if removed or added:
                delta[column] = {
                    "before": sorted(current_set),
                    "after": sorted(recommended_set),
                    "removed": removed,
                    "added": added,
                }

        if dry_run:
            return {
                "config_id": config_id,
                "changed": list(delta.keys()),
                "delta": delta,
                "applied": False,
            }

        if not delta:
            return {
                "config_id": config_id,
                "changed": [],
                "delta": {},
                "applied": False,
                "message": "Config already matches the audit-recommended posture.",
            }

        try:
            for column, recommended in target_posture.items():
                setattr(config, column, list(recommended))
            db.commit()
            db.refresh(config)

            # Cache invalidation so the Rust proxy picks up the new
            # surface arrays on its next read. Mirrors what the PATCH
            # endpoint does after a successful update.
            block_on(invalidate_proxy_cache(request, "guardrail", tenant_id, config_id))

            # (Second phantom API on this endpoint: log_admin_action never
            # existed on UnifiedAdminAuditLogger. Same audit call as every
            # other handler in this file.)
            audit_logger.log(
                action="guardrails.apply_recommended_mode_defaults",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="guardrail_config",
                resource_id=config_id,
                details={"delta": delta},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db,
            )

            logger.info(
                "Applied audit-recommended per-mode surface defaults",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": admin_id,
                    "config_id": config_id,
                    "changed_columns": list(delta.keys()),
                },
            )

            return {
                "config_id": config_id,
                "changed": list(delta.keys()),
                "delta": delta,
                "applied": True,
            }
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(
                "Failed to apply recommended per-mode defaults",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": admin_id,
                    "config_id": config_id,
                    "error": str(e),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to apply recommended per-mode defaults",
            )

    return router


# =============================================================================
# Module-level helpers (outside router factory to avoid closure overhead)
# =============================================================================

def _set_if_changed(config: GuardrailConfig, attr: str, new_value, changes: dict):
    """Set attribute on config and record change if value differs."""
    old_value = getattr(config, attr, None)
    if old_value != new_value:
        changes[attr] = {"old": old_value, "new": new_value}
        setattr(config, attr, new_value)


# Per-surface override fields. Listed here once so each PATCH
# call site stays a single line.
_PER_SURFACE_FIELDS = (
    "pii_per_surface",
    "prompt_injection_per_surface",
    "jailbreak_per_surface",
    "toxic_content_per_surface",
    "secrets_per_surface",
    "banned_words_per_surface",
    "banned_patterns_per_surface",
)


def _set_per_surface_if_present(builtin, config: GuardrailConfig, changes: dict):
    """Wire the per-surface override columns on PATCH.

    Distinguishes 'absent in body' (legacy client / partial PATCH —
    leave column unchanged) from 'explicit value' (write through,
    including null to clear) via Pydantic's ``model_fields_set``.
    Without this distinction an older admin UI omitting per_surface
    would silently wipe the operator's existing overrides.
    """
    sent = getattr(builtin, "model_fields_set", set())
    for attr in _PER_SURFACE_FIELDS:
        if attr in sent:
            _set_if_changed(config, attr, getattr(builtin, attr), changes)


def _config_to_response(config: GuardrailConfig) -> GuardrailConfigResponse:
    """
    Convert a GuardrailConfig DB model to the Pydantic response model.

    Mirrors the pattern of model_manager.to_response() in LLM endpoints,
    but since GuardrailConfig is a singleton per tenant, we use a plain
    function rather than a manager class.
    """
    return GuardrailConfigResponse(
        id=config.id,
        tenant_id=config.tenant_id,
        name=config.name or "Default Guardrails",
        description=config.description,
        is_default=config.is_default if config.is_default is not None else False,
        priority=config.priority or 0,
        # Per-call scope filters
        scope_mcp_server_id=getattr(config, "scope_mcp_server_id", None),
        scope_tool_name=getattr(config, "scope_tool_name", None),
        group_ids=config.group_ids if hasattr(config, 'group_ids') else [],
        groups=[
            GuardrailGroupInfo(
                id=g.id,
                name=g.name,
                display_name=getattr(g, 'display_name', None),
                parent_group_id=getattr(g, 'parent_group_id', None),
                is_active=getattr(g, 'is_active', None),
            )
            for g in (config.groups or [])
        ] if hasattr(config, 'groups') and config.groups else [],
        enabled=config.enabled or False,
        # Streaming output posture (auto|sync|async). MUST flow through —
        # omitting it pins the response to the Pydantic default ("auto")
        # regardless of the stored value, so an operator's sync/async choice
        # silently reverts on the next admin-form load.
        streaming_output_mode=config.streaming_output_mode or "auto",

        # Every surface column on the row flows through this response.
        # Missing any here loses the operator's edit on the next form
        # load: the admin UI falls back to its hard-coded defaults and
        # the toggle appears un-saved even when the DB persisted
        # correctly. Aligned 1:1 with GuardrailSurfaceResponse and
        # GuardrailSurfaceToggles.
        surfaces=GuardrailSurfaceResponse(
            chat_input=config.chat_input_enabled if config.chat_input_enabled is not None else True,
            chat_output=config.chat_output_enabled if config.chat_output_enabled is not None else False,
            embeddings_input=config.embeddings_input_enabled if config.embeddings_input_enabled is not None else True,
            image_gen_input=config.image_gen_input_enabled if config.image_gen_input_enabled is not None else True,
            image_gen_output=config.image_gen_output_enabled if config.image_gen_output_enabled is not None else False,
            audio_tts_input=config.audio_tts_input_enabled if config.audio_tts_input_enabled is not None else True,
            audio_tts_output=config.audio_tts_output_enabled if config.audio_tts_output_enabled is not None else False,
            audio_stt_input=config.audio_stt_input_enabled if config.audio_stt_input_enabled is not None else True,
            audio_stt_output=config.audio_stt_output_enabled if config.audio_stt_output_enabled is not None else True,
            video_gen_input=config.video_gen_input_enabled if config.video_gen_input_enabled is not None else True,
            video_gen_output=config.video_gen_output_enabled if config.video_gen_output_enabled is not None else False,
            batch_input=config.batch_input_enabled if config.batch_input_enabled is not None else True,
            batch_output=config.batch_output_enabled if config.batch_output_enabled is not None else False,
            moderation_input=config.moderation_input_enabled if config.moderation_input_enabled is not None else True,
            mcp_input=config.mcp_input_enabled if config.mcp_input_enabled is not None else True,
            mcp_output=config.mcp_output_enabled if config.mcp_output_enabled is not None else True,
            mcp_registration=getattr(config, "mcp_registration_enabled", None) if getattr(config, "mcp_registration_enabled", None) is not None else True,
            skill_input=config.skill_input_enabled if config.skill_input_enabled is not None else True,
            skill_output=config.skill_output_enabled if config.skill_output_enabled is not None else True,
            a2a_inbound=getattr(config, "a2a_inbound_enabled", None) or False,
            a2a_outbound=getattr(config, "a2a_outbound_enabled", None) or False,
        ),

        builtin=GuardrailBuiltinResponse(
            pii=GuardrailPiiResponse(
                enabled=config.pii_detection_enabled if config.pii_detection_enabled is not None else True,
                action=config.pii_action or "redact",
                provider=config.pii_provider or "built_in",
                categories=config.pii_categories,
                # Presidio score_threshold. NULL → use Presidio's
                # default; built-in provider ignores this field.
                threshold=config.pii_threshold,
                surfaces=config.pii_surfaces if config.pii_surfaces is not None else list(ALL_SURFACES),
                # Forward the per-surface override map verbatim
                # so the admin UI preload reflects what the operator
                # last saved.
                per_surface=config.pii_per_surface,
            ),
            prompt_injection=GuardrailPromptInjectionResponse(
                enabled=config.prompt_injection_enabled if config.prompt_injection_enabled is not None else True,
                action=config.prompt_injection_action or "block",
                sensitivity=config.prompt_injection_sensitivity,
                provider=config.prompt_injection_provider or "built_in",
                surfaces=config.prompt_injection_surfaces if config.prompt_injection_surfaces is not None else list(INPUT_SURFACES),
                per_surface=config.prompt_injection_per_surface,
            ),
            jailbreak=GuardrailJailbreakResponse(
                enabled=config.jailbreak_detection_enabled if config.jailbreak_detection_enabled is not None else True,
                action=config.jailbreak_action or "block",
                provider=config.jailbreak_provider or "built_in",
                surfaces=config.jailbreak_surfaces if config.jailbreak_surfaces is not None else list(INPUT_SURFACES),
                per_surface=config.jailbreak_per_surface,
            ),
            toxic_content=GuardrailToxicContentResponse(
                enabled=config.toxic_content_enabled if config.toxic_content_enabled is not None else False,
                action=config.toxic_content_action or "warn",
                threshold=config.toxic_content_threshold,
                provider=config.toxic_content_provider or "openai_moderation",
                category_thresholds=config.toxic_content_category_thresholds,
                surfaces=config.toxic_content_surfaces if config.toxic_content_surfaces is not None else list(ALL_SURFACES),
                per_surface=config.toxic_content_per_surface,
            ),
            banned_words=config.banned_words or [],
            banned_patterns=config.banned_patterns or [],
            banned_words_match_mode=config.banned_words_match_mode or "substring",
            banned_words_surfaces=config.banned_words_surfaces if config.banned_words_surfaces is not None else list(LLM_MCP_SURFACES),
            banned_patterns_surfaces=config.banned_patterns_surfaces if config.banned_patterns_surfaces is not None else list(LLM_MCP_SURFACES),
            banned_words_per_surface=config.banned_words_per_surface,
            banned_patterns_per_surface=config.banned_patterns_per_surface,
            secrets=GuardrailSecretsResponse(
                enabled=config.secrets_detection_enabled if config.secrets_detection_enabled is not None else False,
                action=config.secrets_action or "redact",
                categories=config.secrets_categories if config.secrets_categories is not None else list(DEFAULT_SECRETS_CATEGORIES),
                surfaces=config.secrets_surfaces if config.secrets_surfaces is not None else list(ALL_SURFACES),
                threshold=config.secrets_threshold if config.secrets_threshold is not None else 0.8,
                per_surface=config.secrets_per_surface,
            ),
        ),

        providers=[
            GuardrailProviderResponse(
                provider=p.get("provider", "unknown"),
                enabled=p.get("enabled", False),
                endpoint=p.get("endpoint"),
                config=p.get("config"),
                config_secrets_stored=p.get("config_secrets_stored", {}),
                # NOTE: api_key and the secrets of `config` are excluded
                # from the response (`provider_secrets.public_entry`).
            )
            for p in map(provider_secrets.public_entry, config.providers or [])
        ],

        max_check_timeout_ms=config.max_check_timeout_ms or 2000.0,
        fail_open=config.fail_open if config.fail_open is not None else False,
        fail_open_overrides=config.fail_open_overrides,
        log_all_checks=config.log_all_checks if config.log_all_checks is not None else False,

        created_at=config.created_at.isoformat() if config.created_at else None,
        updated_at=config.updated_at.isoformat() if config.updated_at else None,
    )
