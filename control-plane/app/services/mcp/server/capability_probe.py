# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The capability probe of a newly wired MCP server.

After an MCP server row exists (a catalog import, a deployment), the gateway
asks the server for its capabilities once, so that the administrator sees at
once whether it answers. This is the probe, and only the probe: it knows
nothing of how the server came to run. A caller that started the server
itself (the Enterprise Edition's deployment drivers) passes `diagnose`, which
is asked for the cause when the probe finds nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.mcp_server import MCPServer
from app.services.outbound_http import error_text

PROBE_ERROR = (
    "MCP capability discovery returned no tools. The server is "
    "reachable on the network but is not answering JSON-RPC "
    "`initialize` correctly."
)

# Seconds the server is given to start accepting connections.
PROBE_DELAY_SECS = 3

# What `diagnose` returns: warnings that name the cause (none when it found
# nothing to say), and an excerpt of what it read, or None.
Diagnosis = Tuple[list, Optional[str]]


@dataclass
class ProbeResult:
    """Outcome of the capability-discovery probe."""

    capabilities_discovered: bool
    deployment_warnings: list[str] = field(default_factory=list)
    container_log_excerpt: Optional[str] = None


async def probe_capabilities(
    *,
    mcp_server: MCPServer,
    db: Session,
    auth_type: str,
    enabled: bool = True,
    skip_probe: bool = False,
    diagnose: Optional[Callable[[], Diagnosis]] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    logger: Optional[BrutorLogger] = None,
    encryption_service: Any = None,
) -> ProbeResult:
    """Run capability discovery against an already-created MCPServer.

    Never raises: a failure lands in `deployment_warnings`, so the calling
    endpoint can answer 200 with a warning instead of 500.

    Args:
        mcp_server: the row to probe (must already be persisted).
        db: session, needed by `MCPServerManager.discover_capabilities`.
        auth_type: `"oauth"` skips the probe: the client-credentials flow
            has not run yet and every probe would be a 401.
        enabled: a disabled server is not probed.
        skip_probe: explicit skip by the caller.
        diagnose: asked when discovery found nothing; its warnings replace
            the generic one, and its excerpt is returned. Not asked when the
            probe is skipped or succeeds. May raise (treated as no diagnosis).
        sleep: the wait before the probe (`asyncio.sleep` by default).
        encryption_service: opens the server's stored static token, which
            the probe sends as the gateway does. Without it a server that
            has a token is not called.
    """
    log = logger or BrutorLogger("mcp-probe")

    can_probe = enabled and auth_type != "oauth" and not skip_probe
    if not can_probe:
        return ProbeResult(capabilities_discovered=False)

    # Give the server a moment to start accepting connections after whatever
    # started it returned. Without this the probe races the bind for a
    # second or two on slower hosts.
    await (sleep or asyncio.sleep)(PROBE_DELAY_SECS)

    capabilities_discovered = False
    try:
        # Lazy import: avoids pulling in the server_manager chain for
        # callers that never reach this branch (e.g. OAuth flows).
        from app.services.mcp.server.mcp_server_manager import MCPServerManager

        server_manager = MCPServerManager(logger=log, encryption_service=encryption_service)
        capabilities_discovered = await server_manager.discover_capabilities(
            server=mcp_server,
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 — surface anything as a warning
        log.warning(
            "Capability discovery raised during probe",
            context={
                "mcp_server_id": mcp_server.id,
                "tenant_id": mcp_server.tenant_id,
                "error": error_text(exc, mcp_server.mcp_url, "MCP server URL"),
            },
        )
        capabilities_discovered = False

    if capabilities_discovered:
        return ProbeResult(capabilities_discovered=True)

    warnings: list = []
    excerpt: Optional[str] = None
    if diagnose is not None:
        try:
            warnings, excerpt = diagnose()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Probe diagnosis failed (non-fatal)",
                context={"mcp_server_id": mcp_server.id, "error": str(exc)},
            )
            warnings, excerpt = [], None

    return ProbeResult(
        capabilities_discovered=False,
        deployment_warnings=list(warnings) or [PROBE_ERROR],
        container_log_excerpt=excerpt,
    )
