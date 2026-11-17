# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Default Guardrails Seeding

Creates the default GuardrailConfig during tenant provisioning with all
built-in checks enabled and sensible defaults:

  - PII detection: enabled, action = redact
  - Prompt injection detection: enabled, action = block
  - Jailbreak detection: enabled, action = block
  - Toxic content detection: enabled, action = warn

All surfaces are enabled for input checking (LLM, MCP, Skill).
Output checking is enabled for Skills only.

The config is assigned to the Default Resource Group so it applies
to all users out of the box.

Called from TenantProvisioner.provision_tenant() after skill/MCP seeding.

Usage:
    from app.database.seed_guardrails import seed_guardrails

    seed_guardrails(db, tenant_id, default_group_id, logger)
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.guardrail_config import (
    DEFAULT_SECRETS_CATEGORIES,
    GuardrailConfig,
    LLM_INPUT_MODE_SURFACES,
    LLM_TEXT_OUTPUT_MODE_SURFACES,
)
from app.database.models.resource_group import ResourceGroup


# Audit-recommended per-check surfaces for new tenants. These narrow
# the jailbreak / prompt_injection / toxic_content checks away from
# operator-controlled corpora (embeddings, batch) and the meta-call
# surface (moderation), where they're category errors. PII + secrets
# remain wide (they're meaningful everywhere).
_CHAT_STYLE_INPUT_MODE_SURFACES = [
    s for s in LLM_INPUT_MODE_SURFACES
    if s not in ("embeddings_input", "batch_input", "moderation_input")
]
# Non-LLM input surfaces stay on the same list so MCP / Skill / A2A
# inbound still get the strict input-side checks.
_AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES = (
    _CHAT_STYLE_INPUT_MODE_SURFACES
    + ["mcp_input", "skill_input", "a2a_inbound"]
)
_AUDIT_RECOMMENDED_WIDE_SURFACES = (
    LLM_INPUT_MODE_SURFACES
    + LLM_TEXT_OUTPUT_MODE_SURFACES
    + ["mcp_input", "mcp_output", "skill_input", "skill_output",
       "a2a_inbound", "a2a_outbound"]
)


def seed_guardrails(
    db: Session,
    tenant_id: str,
    default_group_id: str,
    logger: BrutorLogger,
) -> None:
    """
    Seed the default GuardrailConfig with built-in checks enabled.

    Idempotent -- safe to call on every startup. Checks by stable ID.

    Args:
        db: Database session (caller manages commit)
        tenant_id: Tenant ID
        default_group_id: Default Resource Group ID for binding
        logger: Logger instance
    """
    config_id = f"guardrail-default-{tenant_id}"
    now = datetime.now(timezone.utc)

    existing = db.query(GuardrailConfig).filter(
        GuardrailConfig.id == config_id
    ).first()

    if existing:
        config = existing
    else:
        config = GuardrailConfig(
            id=config_id,
            tenant_id=tenant_id,
            name="Default Guardrails",
            description=(
                "Built-in content safety checks applied to all users. "
                "Detects PII, secrets (AWS / GitHub / OpenAI / JWT / PEM), "
                "prompt injection, jailbreak attempts, and toxic content "
                "across LLM, MCP, and Skill surfaces."
            ),
            is_default=True,
            priority=0,
            enabled=True,

            # ----- Surface toggles -----
            mcp_input_enabled=True,
            mcp_output_enabled=True,
            mcp_registration_enabled=True,
            skill_input_enabled=True,
            skill_output_enabled=True,

            # Per-mode LLM surfaces. Defaults reflect the
            # audit-recommended posture for a new tenant:
            #
            #   * chat_input / chat_output — user-facing, all checks
            #     relevant. chat_output stays OFF by default; operators
            #     opt in via the UI.
            #   * embeddings_input — operator-controlled corpus.
            #     Surface gate stays ON but each check's *_surfaces
            #     array EXCLUDES jailbreak / prompt_injection / toxic
            #     for embeddings_input (set further below in the
            #     post-create per-check surface narrowing).
            #   * image / audio / video — text-style checks on the
            #     input prompt; future NSFW classifiers cover output.
            #   * batch — operator-initiated async; mirrors chat.
            #   * moderation_input — surface gate ON so the
            #     orchestrator enters dispatch, but the orchestrator
            #     hard-bypasses jailbreak / injection / toxic for this
            #     surface regardless of config (recursion guard).
            chat_input_enabled=True,
            chat_output_enabled=False,
            embeddings_input_enabled=True,
            image_gen_input_enabled=True,
            image_gen_output_enabled=False,
            audio_tts_input_enabled=True,
            audio_tts_output_enabled=False,
            audio_stt_input_enabled=True,
            audio_stt_output_enabled=True,
            video_gen_input_enabled=True,
            video_gen_output_enabled=False,
            batch_input_enabled=True,
            batch_output_enabled=False,
            moderation_input_enabled=True,

            # ----- Built-in checks -----
            # PII: redact emails, phones, SSNs, credit cards, API keys
            pii_detection_enabled=True,
            pii_action="redact",

            # Prompt injection: block attempts to override system instructions
            prompt_injection_enabled=True,
            prompt_injection_action="block",
            prompt_injection_sensitivity=0.7,

            # Jailbreak: block attempts to bypass safety guidelines
            jailbreak_detection_enabled=True,
            jailbreak_action="block",

            # Toxic content: warn on hateful/harmful language. Use
            # the bundled profanity wordlist by default so the box
            # ships working without an external API key. Operators
            # can switch to OpenAI Moderation (or another provider)
            # in the admin UI after configuring the relevant API key
            # in External Providers — without this guard the form
            # rejects every save because the default openai_moderation
            # provider has no key bound.
            toxic_content_enabled=True,
            toxic_content_action="warn",
            toxic_content_threshold=0.8,
            toxic_content_provider="built_in",

            # Secrets: redact AWS keys, OpenAI / Anthropic keys, GitHub
            # tokens, GCP service accounts, JWTs, PEM private keys
            # before they leak through a chat reply / MCP tool result /
            # skill output. `redact` masks the value rather than block
            # the request, so an operator quoting a key while triaging
            # an incident isn't refused outright — the audit row still
            # records the hit. Operators with stricter compliance
            # posture (FedRAMP, PCI-DSS) flip this to `block` in the
            # admin UI. Categories default to DEFAULT_SECRETS_CATEGORIES,
            # which excludes the noisy `generic_high_entropy` matcher.
            secrets_detection_enabled=True,
            secrets_action="redact",
            secrets_categories=list(DEFAULT_SECRETS_CATEGORIES),

            # Audit-recommended per-check surfaces. Wide for PII
            # + toxic (and banned_*); narrowed AWAY from operator-
            # controlled corpora for jailbreak + prompt_injection so
            # the embedding bug (KB doc tripping jailbreak on the
            # phrase "ignore previous instructions") never recurs on
            # new tenants. Existing tenants keep their migrated state
            # and must run "Apply recommended per-mode defaults" to
            # opt in.
            pii_surfaces=list(_AUDIT_RECOMMENDED_WIDE_SURFACES),
            toxic_content_surfaces=_CHAT_STYLE_INPUT_MODE_SURFACES + [
                "chat_output", "mcp_input", "mcp_output",
                "skill_input", "skill_output",
                "a2a_inbound", "a2a_outbound",
            ],
            prompt_injection_surfaces=list(_AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES),
            jailbreak_surfaces=list(_AUDIT_RECOMMENDED_STRICT_INPUT_SURFACES),
            secrets_surfaces=list(_AUDIT_RECOMMENDED_WIDE_SURFACES),
            banned_words_surfaces=_CHAT_STYLE_INPUT_MODE_SURFACES + [
                "chat_output", "mcp_input", "mcp_output",
            ],
            banned_patterns_surfaces=_CHAT_STYLE_INPUT_MODE_SURFACES + [
                "chat_output", "mcp_input", "mcp_output",
            ],

            # No banned words/patterns by default
            banned_words=[],
            banned_patterns=[],

            # No external providers by default
            providers=[],

            # ----- Performance -----
            max_check_timeout_ms=2000.0,
            fail_open=False,
            log_all_checks=False,

            created_at=now,
            updated_at=now,
        )

        db.add(config)
        db.flush()

        logger.info("Created default guardrail config", context={
            "config_id": config_id,
            "tenant_id": tenant_id,
            "checks": {
                "pii": "redact",
                "prompt_injection": "block",
                "jailbreak": "block",
                "toxic_content": "warn",
            },
        })

    # ------------------------------------------------------------------
    # Bind to Default Resource Group
    # ------------------------------------------------------------------
    default_group = db.query(ResourceGroup).filter(
        ResourceGroup.id == default_group_id
    ).first()

    if default_group and config not in default_group.guardrail_configs:
        default_group.guardrail_configs.append(config)
        db.flush()

        logger.info("Bound guardrail config to Default resource group", context={
            "config_id": config.id, "group_id": default_group_id,
        })
    elif default_group:
        pass  # Already bound
