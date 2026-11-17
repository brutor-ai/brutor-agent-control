# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What provisioning a tenant creates, as an ordered list of named steps.

`TENANT_PROVISIONING` is everything `TenantProvisioner.provision_tenant`
(`app/services/tenant/tenant_provisioning.py`) does for a tenant, in order:
roles, the first admin, the resource groups, then the seeded content. The
provisioner builds the `ProvisioningContext`, runs the list and returns the
result; it holds no step of its own. The same list runs for the default tenant
at install and for every tenant created through the Admin API.

A step is a function of the `ProvisioningContext`. Everything runs in the
caller's session and the caller commits. The roles, the admin and the two
resource groups must succeed, so those steps raise. Every later step goes
through `run_seed_with_retry`: a failure is recorded in `result.warnings` and
provisioning continues.

Later steps read what earlier ones wrote into `result` (`role_ids`,
`default_group_id`), so the order is part of the contract.

These are the Community Edition's steps: none names a
feature. The Enterprise Edition's steps (the MCP registries, the cache
defaults, the portal user) are its extension's, each tagged with its feature
(`ProvisioningStep.feature`), and run after these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from sqlalchemy.orm import Session

from app.constants.tenant import TenantDefaultRole
from app.core.logging_system import BrutorLogger
from app.database.models.tenant import Tenant
from app.services.tenant.tenant_provisioning import ProvisioningResult, TenantProvisioner, run_seed_with_retry
from app.env_vars import BRUTOR_DEFAULT_JUDGE_MODEL, BRUTOR_EMBEDDING_MODEL

# The models tried first, in this order, as a new tenant's default judge:
# cheap and fast ones, because a judge runs on every governed request that
# hits a policy. They are ids of the seeded catalog
# (tests/test_model_defaults_in_catalog.py holds them to it); neither is
# enabled by default, so they apply once an operator has enabled one, and a
# tenant without them gets its first enabled chat model.
DEFAULT_JUDGE_MODEL_CANDIDATES = ("gpt-6-luna", "claude-haiku-5-5")


@dataclass
class ProvisioningContext:
    """What a provisioning step works with.

    `result` is what the provisioner returns; the steps fill it in. The
    remaining fields are the arguments of `provision_tenant`.
    """

    provisioner: TenantProvisioner
    db: Session
    tenant: Tenant
    result: ProvisioningResult
    admin_username: str
    admin_password: str
    admin_email: str
    admin_full_name: str = "Tenant Administrator"
    admin_avatar_url: str = ""
    seed_llms: bool = True
    portal_user: Optional[dict] = None

    @property
    def logger(self) -> BrutorLogger:
        return self.provisioner.logger


@dataclass(frozen=True)
class ProvisioningStep:
    """One named step of tenant provisioning.

    `feature` is the Enterprise Edition feature the step belongs to, or None
    for Community.
    """

    name: str
    run: Callable[[ProvisioningContext], None]
    feature: Optional[str] = None


def run_provisioning_steps(context: ProvisioningContext, steps: Sequence[ProvisioningStep]) -> None:
    """Run the steps in order. A step that raises stops the run."""
    for step in steps:
        step.run(context)


def _roles(ctx: ProvisioningContext) -> None:
    """The tenant's default roles."""
    provisioner = ctx.provisioner
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result

    # ================================================================
    # 1. Seed Tenant Roles (RBAC)
    # ================================================================
    result.role_ids = provisioner._seed_roles(db, tenant.id)


def _tenant_admin(ctx: ProvisioningContext) -> None:
    """The tenant's first admin."""
    provisioner = ctx.provisioner
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    admin_username = ctx.admin_username
    admin_password = ctx.admin_password
    admin_email = ctx.admin_email
    admin_full_name = ctx.admin_full_name
    admin_avatar_url = ctx.admin_avatar_url

    # ================================================================
    # 2. Create Default Tenant Admin
    # ================================================================
    admin_role_id = result.role_ids.get(TenantDefaultRole.ADMIN_USER.value)
    if not admin_role_id:
        raise ValueError("Tenant Admin role not found — role seeding failed")

    result.admin_id, result.admin_username = provisioner._create_tenant_admin(
        db=db,
        tenant_id=tenant.id,
        role_id=admin_role_id,
        username=admin_username,
        password=admin_password,
        email=admin_email,
        full_name=admin_full_name,
        avatar_url=admin_avatar_url,
    )


def _default_resource_group(ctx: ProvisioningContext) -> None:
    """The Default resource group."""
    provisioner = ctx.provisioner
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result

    # ================================================================
    # 3. Create Default Resource Group
    # ================================================================
    result.default_group_id = provisioner._create_default_resource_group(db, tenant.id)


def _unbound_resource_group(ctx: ProvisioningContext) -> None:
    """The Unbound system group."""
    provisioner = ctx.provisioner
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result

    # 3a. The Unbound group: where the core bills the platform service
    # key's calls (health checks, judge, embeddings).
    result.unbound_group_id = provisioner._create_unbound_resource_group(db, tenant.id)


def _a2a_signing_key(ctx: ProvisioningContext) -> None:
    """The tenant's agent card signing key."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 3b. Generate A2A signing keypair
    # Every tenant gets an Ed25519 keypair on provisioning so admins
    # can sign agent cards immediately — no post-install step, no
    # chicken-and-egg if an admin wants to sign a card in the first
    # session. Failure is non-fatal: the tenant is still usable, and
    # the admin can retry sign/public-key operations later which
    # will lazily create the key via get_or_create_tenant_key().
    # ================================================================
    def _seed_a2a_keypair():
        from app.services.a2a.card_signing import get_or_create_tenant_key
        from app.services.config_manager import ConfigManager
        from app.services.oauth.encryption_service import EncryptionService
        encryption_service = EncryptionService(
            config_manager=ConfigManager(logger=logger, validate=False),
            logger=logger,
        )
        get_or_create_tenant_key(db, tenant.id, encryption_service)

    run_seed_with_retry(
        db, "A2A tenant signing key",
        _seed_a2a_keypair, logger, result.warnings,
    )


def _system_mcp_servers(ctx: ProvisioningContext) -> None:
    """The system MCP servers."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4. Seed System MCP Servers
    # ================================================================
    from app.database.seed_system_servers import seed_system_mcp_servers
    if run_seed_with_retry(
        db, "System server seeding",
        lambda: seed_system_mcp_servers(
            db=db,
            tenant_id=tenant.id,
            default_group_id=result.default_group_id,
            logger=logger,
        ),
        logger, result.warnings,
    ):
        result.system_servers_seeded = True


def _agent_skills(ctx: ProvisioningContext) -> None:
    """The starter agent skills."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4b. Seed Default Agent Skills
    # ================================================================
    from app.database.seed_agent_skills import seed_agent_skills
    run_seed_with_retry(
        db, "Agent skills seeding",
        lambda: seed_agent_skills(
            db=db,
            tenant_id=tenant.id,
            default_group_id=result.default_group_id,
            logger=logger,
        ),
        logger, result.warnings,
    )


def _mcp_servers(ctx: ProvisioningContext) -> None:
    """The example MCP servers."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4c. Seed Default MCP Servers (GitHub, HubSpot)
    # ================================================================
    from app.database.seed_mcp_servers import seed_mcp_servers
    run_seed_with_retry(
        db, "MCP server seeding",
        lambda: seed_mcp_servers(
            db=db,
            tenant_id=tenant.id,
            default_group_id=result.default_group_id,
            logger=logger,
        ),
        logger, result.warnings,
    )


def _guardrails(ctx: ProvisioningContext) -> None:
    """The default guardrails."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4d. Seed Default Guardrails
    # ================================================================
    from app.database.seed_guardrails import seed_guardrails
    run_seed_with_retry(
        db, "Guardrail seeding",
        lambda: seed_guardrails(
            db=db,
            tenant_id=tenant.id,
            default_group_id=result.default_group_id,
            logger=logger,
        ),
        logger, result.warnings,
    )



def _mcp_catalog(ctx: ProvisioningContext) -> None:
    """The global MCP server catalogue."""
    db = ctx.db
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4g. Seed MCP Server Catalog
    # ================================================================
    # The MCP catalog is GLOBAL now (system entries, tenant_id=NULL).
    # This seeder is idempotent and no-ops once the global catalog
    # exists, so it stays on the per-tenant provisioning path but never
    # copies the catalog per tenant.
    from app.database.seed_mcp_catalog import seed_mcp_catalog
    run_seed_with_retry(
        db, "MCP catalog seeding",
        lambda: seed_mcp_catalog(db=db, logger=logger),
        logger, result.warnings,
    )


def _shared_mcp_servers(ctx: ProvisioningContext) -> None:
    """The MCP servers the operator shares with every tenant."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4g.5 Seed Shared MCP Servers (e.g. brutor-mcp-app-map)
    # ================================================================
    # Operator-supplied list of MCP servers — typically deployed
    # as containers in the same compose / ECS cluster — that get
    # registered as MCPServer + ServerConfig + group binding rows
    # for every new tenant. Keyed off the BRUTOR_SHARED_MCP_SERVERS
    # env var. Empty / unset → no-op.
    from app.database.seed_shared_mcp_servers import seed_shared_mcp_servers
    run_seed_with_retry(
        db, "Shared MCP server seeding",
        lambda: seed_shared_mcp_servers(
            db=db,
            tenant_id=tenant.id,
            default_group_id=result.default_group_id,
            logger=logger,
        ),
        logger, result.warnings,
    )


def _sample_agent_cards(ctx: ProvisioningContext) -> None:
    """The sample agent cards."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 4h. Seed Sample Agent Cards (hello-world A2A)
    # ================================================================
    from app.database.seed_agent_cards import seed_sample_agent_cards
    run_seed_with_retry(
        db, "Sample agent card seeding",
        lambda: seed_sample_agent_cards(
            db=db,
            tenant_id=tenant.id,
            default_group_id=result.default_group_id,
            logger=logger,
        ),
        logger, result.warnings,
    )


def _llm_models(ctx: ProvisioningContext) -> None:
    """The default LLM models, unless the caller declined them."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger
    seed_llms = ctx.seed_llms

    # ================================================================
    # 5. Seed Default LLM Models
    # ================================================================
    if seed_llms:
        from app.database.models.llm_model import LLModel
        from app.database.init_llms import create_llm_models

        def _seed_llms():
            existing_llm_count = db.query(LLModel).filter(
                LLModel.tenant_id == tenant.id
            ).count()

            if existing_llm_count > 0:
                result.llm_count = existing_llm_count
            else:
                llm_ids = create_llm_models(
                    db=db,
                    tenant_id=tenant.id,
                    default_group_id=result.default_group_id,
                    logger=logger,
                )
                result.llm_count = len(llm_ids)

        run_seed_with_retry(
            db, "LLM seeding", _seed_llms, logger, result.warnings,
        )


def _default_embedding_model(ctx: ProvisioningContext) -> None:
    """The tenant's default embedding model, when none is set."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 5b. Set tenant default embedding model
    # ================================================================
    # Prefer OpenAI text-embedding-3-small (1536-dim, cheap, high quality).
    # Fall back to any other enabled embedding model if it isn't seeded
    # (e.g. Ollama-only tenants). Only set if the tenant doesn't already
    # have a default — never overwrite an admin's explicit choice.
    def _set_default_embedding_model():
        from app.database.models.llm_model import LLModel

        # Re-fetch tenant in case another seed step already touched it.
        current = db.query(Tenant).filter(Tenant.id == tenant.id).first()
        if current is None or current.default_embedding_model_id:
            return

        # Deployment preference wins when set. Lets the trial startup
        # script steer the tenant default toward whichever embedding
        # model the operator picked (e.g. `nomic-embed-text` when
        # they chose local Ollama, or `text-embedding-3-small` when
        # they're bringing their own OpenAI key). Falls through to
        # the historical OpenAI-first heuristic when unset or when
        # the named model isn't enabled on this tenant.
        env_preference = BRUTOR_EMBEDDING_MODEL.string() or ""
        preferred = None
        if env_preference:
            preferred = (
                db.query(LLModel)
                .filter(
                    LLModel.tenant_id == tenant.id,
                    LLModel.mode == "embedding",
                    LLModel.enabled.is_(True),
                    LLModel.model_name == env_preference,
                )
                .first()
            )

        if preferred is None:
            preferred = (
                db.query(LLModel)
                .filter(
                    LLModel.tenant_id == tenant.id,
                    LLModel.mode == "embedding",
                    LLModel.enabled.is_(True),
                    LLModel.model_name == "text-embedding-3-small",
                )
                .first()
            )
        if preferred is None:
            preferred = (
                db.query(LLModel)
                .filter(
                    LLModel.tenant_id == tenant.id,
                    LLModel.mode == "embedding",
                    LLModel.enabled.is_(True),
                )
                .order_by(LLModel.model_name.asc())
                .first()
            )

        if preferred is None:
            logger.info(
                "No enabled embedding model found — leaving tenant.default_embedding_model_id unset",
                context={"tenant_id": tenant.id},
            )
            return

        current.default_embedding_model_id = preferred.id
        db.flush()
        logger.info(
            "Set tenant default embedding model",
            context={
                "tenant_id": tenant.id,
                "model_id": preferred.id,
                "model_name": preferred.model_name,
            },
        )

    run_seed_with_retry(
        db, "Tenant default embedding model",
        _set_default_embedding_model, logger, result.warnings,
    )


def _default_judge_model(ctx: ProvisioningContext) -> None:
    """The tenant's default judge model, when none is set."""
    db = ctx.db
    tenant = ctx.tenant
    result = ctx.result
    logger = ctx.logger

    # ================================================================
    # 5c. Set tenant default judge model (semantic policies)
    # ================================================================
    # Mirrors 5b. Without this, a semantic policy that doesn't pin a
    # judge had no operator-chosen model to fall back on, and the
    # runtime picked "first enabled chat model by name" — a pick that
    # changed whenever a model was added or renamed, could land on an
    # expensive frontier model, and could name one no resource group
    # had bound (403 → the policy silently fails open). Setting a
    # tenant default makes the judge an operator decision, and lets
    # system_model_bindings auto-bind it to the policy's groups.
    #
    # Idempotent and non-destructive: an existing default is never
    # overwritten, so this also backfills tenants provisioned before
    # judge defaults existed.
    def _set_default_judge_model():
        from app.database.models.llm_model import LLModel

        current = db.query(Tenant).filter(Tenant.id == tenant.id).first()
        if current is None or current.default_judge_model_id:
            return

        def _pick(**filters):
            q = db.query(LLModel).filter(
                LLModel.tenant_id == tenant.id,
                LLModel.enabled.is_(True),
                # `mode IS NULL` is legacy chat — same predicate the
                # runtime judge lookup uses.
                (LLModel.mode == "chat") | (LLModel.mode.is_(None)),
            )
            for attr, value in filters.items():
                q = q.filter(getattr(LLModel, attr) == value)
            return q.order_by(LLModel.model_name.asc()).first()

        # Deployment preference wins, exactly like the default embedding model's.
        env_preference = BRUTOR_DEFAULT_JUDGE_MODEL.string() or ""
        preferred = _pick(model_name=env_preference) if env_preference else None

        # A judge runs on every governed request that hits a policy, so
        # default to something cheap and fast rather than the most
        # capable model the tenant happens to have.
        if preferred is None:
            for candidate in DEFAULT_JUDGE_MODEL_CANDIDATES:
                preferred = _pick(model_name=candidate)
                if preferred is not None:
                    break

        if preferred is None:
            preferred = _pick()

        if preferred is None:
            logger.info(
                "No enabled chat model found — leaving tenant.default_judge_model_id unset",
                context={"tenant_id": tenant.id},
            )
            return

        current.default_judge_model_id = preferred.id
        db.flush()
        logger.info(
            "Set tenant default judge model",
            context={
                "tenant_id": tenant.id,
                "model_id": preferred.id,
                "model_name": preferred.model_name,
            },
        )

    run_seed_with_retry(
        db, "Tenant default judge model",
        _set_default_judge_model, logger, result.warnings,
    )





TENANT_PROVISIONING: Sequence[ProvisioningStep] = (
    ProvisioningStep("roles", _roles),
    ProvisioningStep("tenant_admin", _tenant_admin),
    ProvisioningStep("default_resource_group", _default_resource_group),
    ProvisioningStep("unbound_resource_group", _unbound_resource_group),
    ProvisioningStep("a2a_signing_key", _a2a_signing_key),
    ProvisioningStep("system_mcp_servers", _system_mcp_servers),
    ProvisioningStep("agent_skills", _agent_skills),
    ProvisioningStep("mcp_servers", _mcp_servers),
    ProvisioningStep("guardrails", _guardrails),
    ProvisioningStep("mcp_catalog", _mcp_catalog),
    ProvisioningStep("shared_mcp_servers", _shared_mcp_servers),
    ProvisioningStep("sample_agent_cards", _sample_agent_cards),
    ProvisioningStep("llm_models", _llm_models),
    ProvisioningStep("default_embedding_model", _default_embedding_model),
    ProvisioningStep("default_judge_model", _default_judge_model),
)
