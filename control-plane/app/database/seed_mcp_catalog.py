# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Catalog Seeding

Seeds the mcp_catalog_entries table with pre-built MCP server configurations
adapted from the Obot MCP Catalog (80 entries). Each entry is a template
that admins can browse and import to quickly create MCP server + OAuth +
ServerConfig records.

Idempotent — system entries are GLOBAL (tenant_id NULL) keyed on the stable
id `mcp-cat-system-{slug}`, so this seeds ONCE platform-wide regardless of how
many tenants exist.

Called (idempotently) from TenantProvisioner.provision_tenant() after MCP
registry seeding — it no-ops once the global catalog already exists, so it is
safe to leave on the per-tenant provisioning path; the catalog itself is no
longer copied per tenant.

Usage:
    from app.database.seed_mcp_catalog import seed_mcp_catalog
    seed_mcp_catalog(db=db, logger=logger)
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.mcp_catalog_entry import MCPCatalogEntry


def seed_mcp_catalog(
    db: Session,
    logger: Optional[BrutorLogger] = None,
    tenant_id: Optional[str] = None,  # noqa: ARG001 — accepted+ignored for back-compat
) -> None:
    """
    Seed the GLOBAL MCP server catalog (system entries, tenant_id=NULL).

    Idempotent — safe to call on every startup / tenant provision. System
    entries are keyed on the stable global id `mcp-cat-system-{slug}`; a
    missing row is inserted, an existing one is refreshed in place.

    Args:
        db: Database session (caller manages commit)
        logger: Logger instance (optional)
        tenant_id: Deprecated/ignored. Accepted only so legacy call sites
                   that still pass a tenant don't break — the catalog is
                   global now and never tenant-scoped.
    """
    from app.database.catalog_data.mcp_catalog_entries import get_catalog_entries

    entries = get_catalog_entries()
    created_count = 0
    refreshed_count = 0

    for entry_data in entries:
        slug = entry_data["slug"]
        stable_id = f"mcp-cat-system-{slug}"

        existing = db.query(MCPCatalogEntry).filter(
            MCPCatalogEntry.id == stable_id
        ).first()

        if existing:
            # Refresh descriptive fields in place — the builtin seed is
            # authoritative for system entries. Don't touch `enabled` so an
            # operator's toggle survives a re-seed.
            existing.tenant_id = None
            existing.name = entry_data["name"]
            existing.short_description = entry_data.get("short_description")
            existing.description = entry_data.get("description")
            existing.icon_url = entry_data.get("icon_url")
            existing.repo_url = entry_data.get("repo_url")
            existing.runtime = entry_data["runtime"]
            existing.remote_config = entry_data.get("remote_config")
            existing.containerized_config = entry_data.get("containerized_config")
            existing.env_vars = entry_data.get("env_vars", [])
            existing.tool_preview = entry_data.get("tool_preview", [])
            existing.categories = entry_data.get("categories", [])
            existing.metadata_extra = entry_data.get("metadata_extra")
            existing.auth_pattern = entry_data.get("auth_pattern", "none")
            existing.source = "system"
            refreshed_count += 1
            continue

        catalog_entry = MCPCatalogEntry(
            id=stable_id,
            tenant_id=None,
            name=entry_data["name"],
            slug=slug,
            short_description=entry_data.get("short_description"),
            description=entry_data.get("description"),
            icon_url=entry_data.get("icon_url"),
            repo_url=entry_data.get("repo_url"),
            runtime=entry_data["runtime"],
            remote_config=entry_data.get("remote_config"),
            containerized_config=entry_data.get("containerized_config"),
            env_vars=entry_data.get("env_vars", []),
            tool_preview=entry_data.get("tool_preview", []),
            categories=entry_data.get("categories", []),
            metadata_extra=entry_data.get("metadata_extra"),
            auth_pattern=entry_data.get("auth_pattern", "none"),
            source="system",
            enabled=True,
        )

        db.add(catalog_entry)
        db.flush()
        created_count += 1

    if logger is not None:
        logger.info(
            "MCP catalog seeding complete (global)",
            context={
                "created": created_count,
                "refreshed": refreshed_count,
                "total_available": len(entries),
            },
        )
