# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Shared MCP Server Seeding

Registers the operator-supplied list of "shared" MCP servers on
every newly-provisioned tenant. Reads from the
``BRUTOR_SHARED_MCP_SERVERS`` env var (JSON array).

A "shared" MCP server is one that lives outside the tenant — same
URL serves every tenant — but has a per-tenant MCPServer +
ServerConfig + Default-group binding so the tenant's gateway-core
can address it via standard config. Multi-tenant isolation is
enforced at the gateway boundary; the shared server itself is
stateless and trusts the gateway via either no-auth (compose) or a
shared bearer (production).

The Map MCP App (``brutor-mcp-app-map``) is the canonical user — it
ships with the trial bundle as a single shared
container, registered into every tenant via this seeder.

Idempotent: keyed on stable id ``shared-mcp-{slug}-{tenant_id}``.
Re-running upserts the binding only — does not stomp operator edits
to the MCPServer row, matching the create-once-respect-edits stance
in feedback_seeds_create_once.md.

Wired into TenantProvisioner after seed_mcp_catalog so any new
tenant comes up with these MCP servers ready to use.
"""

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.resource_mixins import SYSTEM_ACTOR
from app.database.seed_icons import ICON_MAP
from app import env_registry
from app.env_vars import BRUTOR_SHARED_MCP_SERVERS

# The variable the operator sets (compose, a task definition, ...) to a
# JSON array. Empty / unset / "[]" → no-op.
ENV_VAR = BRUTOR_SHARED_MCP_SERVERS.name

# Slug → base64 PNG icon mapping. Same Brutor brand-blue palette as
# the system MCP servers (Agent Skills + Web Search) so the bundled
# shared servers visually belong to the same family. Slugs not in
# this map ship with no embedded icon — the spec's `icon` field is
# then surfaced as a string emoji or external URL via ServerConfig.
_BUILTIN_ICONS: Dict[str, str] = {
    "map": ICON_MAP,
}


def _load_spec() -> List[Dict[str, Any]]:
    """The entries of the variable that carry a `slug`. A value that is
    not a JSON array of objects is refused where the variable is read (and
    at start), so it never gets here as an empty list."""
    raw = BRUTOR_SHARED_MCP_SERVERS.string()
    if not raw:
        return []
    return [s for s in json.loads(raw) if s.get("slug")]


def seed_shared_mcp_servers(
    db: Session,
    tenant_id: str,
    default_group_id: Optional[str],
    logger: BrutorLogger,
    encryption_service: Any = None,
) -> None:
    """Seed each spec entry into MCPServer + ServerConfig + group
    binding. Idempotent — safe to call on every startup.

    `encryption_service` encrypts a spec's `auth_token` before it is
    stored (the installation's own by default). A token that cannot be
    encrypted is not stored: the entry is skipped with a warning."""
    specs = _load_spec()
    if not specs:
        logger.debug(
            f"{ENV_VAR} unset or empty; no shared MCP servers to seed",
            context={"tenant_id": tenant_id},
        )
        return

    if encryption_service is None and any(spec.get("auth_token") for spec in specs):
        encryption_service = _installation_encryption_service(logger)

    for spec in specs:
        try:
            _seed_one(db, tenant_id, default_group_id, spec, logger, encryption_service)
        except Exception as e:
            # Don't let one bad spec block the others. The seed
            # framework's outer retry catches operational errors;
            # this catches bad data in a single row.
            logger.warning(
                "Skipping shared MCP server seed entry due to error",
                context={
                    "slug": spec.get("slug"),
                    "tenant_id": tenant_id,
                    "error": str(e),
                },
            )


def _installation_encryption_service(logger: BrutorLogger) -> Any:
    """The encryption service of this installation, or `None` where it has
    no usable key."""
    from app.services.config_manager import ConfigManager
    from app.services.oauth.encryption_service import EncryptionService

    try:
        return EncryptionService(config_manager=ConfigManager(logger=logger, validate=False), logger=logger)
    except Exception as e:  # noqa: BLE001 - no key, or one that is refused
        logger.warning(
            "No encryption service for shared MCP server tokens", context={"error": type(e).__name__}
        )
        return None


def _seed_one(
    db: Session,
    tenant_id: str,
    default_group_id: Optional[str],
    spec: Dict[str, Any],
    logger: BrutorLogger,
    encryption_service: Any = None,
) -> None:
    slug = str(spec["slug"]).strip()
    if not slug:
        return

    server_id = f"shared-mcp-{slug}-{tenant_id}"
    config_id = f"shared-mcp-cfg-{slug}-{tenant_id}"
    name = str(spec.get("name") or slug.replace("-", " ").title())
    description = spec.get("description") or ""
    base_url = str(spec.get("base_url") or "").strip()
    if not base_url:
        logger.warning(
            "Shared MCP server spec missing base_url; skipping",
            context={"slug": slug, "tenant_id": tenant_id},
        )
        return

    auth_type = str(spec.get("auth_type") or "none").lower()
    # auth_token may carry a `${NAME}` placeholder, so that a hosted
    # deployment can inject the secret under a name of its own without
    # writing the value into the variable. A placeholder whose variable is
    # not set is left as it is written, and refused below.
    raw_auth_token = spec.get("auth_token")
    auth_token = (
        env_registry.expand_references(raw_auth_token)
        if isinstance(raw_auth_token, str) and raw_auth_token
        else raw_auth_token
    )
    if isinstance(auth_token, str) and "${" in auth_token:
        # Unresolved placeholder — the operator forgot to wire the
        # secret. Don't quietly write a garbage token onto the row;
        # warn loudly and skip the auth_token (server will be created
        # with auth_type='bearer' but no token, surfaced as a 401 from
        # the upstream when used).
        logger.warning(
            "Shared MCP server auth_token has unresolved env-var placeholder; "
            "leaving token blank. Set the env var referenced in the JSON.",
            context={"slug": slug, "tenant_id": tenant_id},
        )
        auth_token = None
    if auth_token and encryption_service is None:
        # A token is never stored in clear, and a bearer server without
        # its token would be registered broken: the entry is left out.
        logger.warning(
            "Shared MCP server has an auth_token and this control plane has no encryption service "
            "(ENCRYPTION_KEY); the server is not registered.",
            context={"slug": slug, "tenant_id": tenant_id},
        )
        return
    stored_token = encryption_service.encrypt(auth_token) if auth_token else None
    timeout = float(spec.get("timeout") or 30.0)
    icon = spec.get("icon")  # Optional emoji or icon URL

    # ── 1. MCPServer ───────────────────────────────────────────────────
    server = db.query(MCPServer).filter(MCPServer.id == server_id).first()
    if server is None:
        # Embed the Brutor-branded PNG when this slug has a built-in
        # icon (currently `map`). For other slugs the spec's `icon`
        # passes through as text (emoji / URL) on the ServerConfig.
        icon_b64 = _BUILTIN_ICONS.get(slug)
        icon_bytes = base64.b64decode(icon_b64) if icon_b64 else None
        icon_kwargs = {}
        if icon_bytes:
            icon_kwargs.update(
                icon_data=icon_bytes,
                icon_mime_type="image/png",
                icon_size=len(icon_bytes),
                icon_hash=hashlib.sha256(icon_bytes).hexdigest(),
                icon_uploaded_at=datetime.now(timezone.utc),
            )

        server = MCPServer(
            id=server_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            base_url=base_url,
            mcp_endpoint_path="",  # base_url already carries `/mcp`
            auth_type=auth_type,
            auth_token=stored_token,
            timeout=timeout,
            enabled=True,
            # Shared MCP servers we ship (currently the Map app) carry an
            # MCP Apps UI bundle by design — set the governance flag
            # explicitly so the gateway-core resource endpoint serves the
            # iframe HTML instead of 403'ing. Operators can flip this off
            # later in the admin UI for compliance-bound tenants. The
            # SQLAlchemy column default already does this on a clean DB,
            # but rows written before the column existed end up
            # with `false` — this line stops that from biting.
            mcp_apps_enabled=True,
            capabilities={
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "prompts": [],
            },
            discovered_at=datetime.now(timezone.utc),
            server_info={"name": name, "version": "shared"},
            health_status="unknown",
            consecutive_health_failures=0,
            idp_id=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            **icon_kwargs,
        )
        db.add(server)
        db.flush()
        logger.info(
            "Created shared MCP server",
            context={
                "server_id": server.id,
                "tenant_id": tenant_id,
                "slug": slug,
                "base_url": base_url,
            },
        )

    # ── 2. ServerConfig ────────────────────────────────────────────────
    config = db.query(ServerConfig).filter(ServerConfig.id == config_id).first()
    if config is None:
        config = ServerConfig(
            id=config_id,
            tenant_id=tenant_id,
            mcp_server_id=server.id,
            oauth_client_id=None,
            name=name,
            description=description,
            priority=20,
            is_default=False,
            enabled=True,
            display_name=name,
            display_description=description,
            icon_url=icon if isinstance(icon, str) and icon.startswith("http") else None,
            usage_count=0,
            created_by=SYSTEM_ACTOR,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(config)
        db.flush()
        logger.info(
            "Created shared MCP server config",
            context={
                "config_id": config.id,
                "server_id": server.id,
                "tenant_id": tenant_id,
            },
        )

    # ── 3. Bind to the requested resource group ───────────────────────
    target_group_name = str(spec.get("default_group") or "Default").strip()
    target_group_id = _resolve_group_id(
        db, tenant_id, target_group_name, default_group_id
    )
    if target_group_id is None:
        logger.warning(
            "No matching resource group for shared MCP server binding; skipping bind",
            context={
                "slug": slug,
                "tenant_id": tenant_id,
                "target_group": target_group_name,
            },
        )
        return

    existing_binding = db.execute(
        resource_group_server_configs.select().where(
            (resource_group_server_configs.c.resource_group_id == target_group_id)
            & (resource_group_server_configs.c.server_config_id == config.id)
        )
    ).first()
    if existing_binding is not None:
        return

    db.execute(
        resource_group_server_configs.insert().values(
            id=f"rgsc-shared-{slug}-{tenant_id}",
            resource_group_id=target_group_id,
            server_config_id=config.id,
            created_by=SYSTEM_ACTOR,
        )
    )
    logger.info(
        "Bound shared MCP server to resource group",
        context={
            "slug": slug,
            "config_id": config.id,
            "group_id": target_group_id,
            "tenant_id": tenant_id,
        },
    )


def _resolve_group_id(
    db: Session,
    tenant_id: str,
    group_name: str,
    default_group_id: Optional[str],
) -> Optional[str]:
    """Find the group by name within the tenant. Falls back to the
    provisioner-supplied default_group_id when name resolution
    misses (covers Default, which uses a stable id elsewhere)."""
    row = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.name == group_name,
        )
        .first()
    )
    if row is not None:
        return row.id
    return default_group_id
