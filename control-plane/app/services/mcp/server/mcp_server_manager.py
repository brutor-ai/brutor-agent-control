# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Manager
Consolidated service for managing MCP servers: testing, discovery, health checks
Icon management is handled directly in endpoints using IconManager
"""

import socket
import json
import httpx
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from app.services.outbound_http import OutboundRefused, error_text, find_refusal, outbound_async_client
from app.services.outbound_policy import ALLOW_SETTING, refusal_message
from app.constants.server import ServerStatusType
from app.constants.system_server import is_system_server
from app.database.models.mcp_server import MCPServer
from app.core.logging_system import BrutorLogger
from app.services.mcp.server import server_token
from app.services.mcp.server.oauth_discovery_helper import OAuthDiscoveryHelper
from app.services.mcp.tool_snapshot import normalize_mcp_capabilities, normalize_tools


def policy_mcp_http_client(headers=None, timeout=None, auth=None):
    """The MCP SDK's client factory (`create_mcp_http_client`), with the
    outbound policy's transport: an MCP server's URL is an administrator's
    destination."""
    if timeout is None:
        timeout = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    kwargs = {"timeout": timeout}
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return outbound_async_client(**kwargs)


class MCPServerManager:
    """
    Unified service for MCP server management

    Handles:
    - Connection testing and validation
    - Capability discovery
    - Health monitoring
    - Response building for API
    """

    def __init__(
        self,
        logger: BrutorLogger,
        encryption_service: Any = None,
    ):
        self.logger = logger
        # Opens a server's stored static token for capability discovery.
        self.encryption_service = encryption_service

    # ========================================================================
    # Connection Testing
    # ========================================================================

    async def test_connection(
            self,
            server: MCPServer,
            timeout: float = 10.0,
            db: Optional[Session] = None,
            access_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Connection test with OAuth/OIDC discovery and existing IdP check

        Checks for existing IdP by issuer before suggesting new config
        """
        start_time = time.time()
        oauth_helper = OAuthDiscoveryHelper(self.logger)

        # Step 1: Basic HTTP connectivity test
        try:
            connectivity_result = await self._test_basic_http_connectivity(
                server.mcp_url, timeout
            )

            if not connectivity_result["connected"]:
                duration_ms = (time.time() - start_time) * 1000
                return {
                    "status": "error",
                    "connected": False,
                    "error_type": connectivity_result["error_type"],
                    "error": connectivity_result["message"],
                    "message": connectivity_result["message"],  # UI compatibility
                    "duration_ms": duration_ms,
                    "suggestions": connectivity_result.get("suggestions", [])
                }

            # Extract www-authenticate header if present
            www_auth_header = connectivity_result.get("www_authenticate")
            oauth_discovery = None
            suggested_idp = None
            existing_idp = None

            if www_auth_header:
                # Parse www-authenticate header
                auth_info = oauth_helper.parse_www_authenticate(www_auth_header)

                # Discovery with integrated existing IdP check scoped to tenant
                discovered_config = await oauth_helper.discover_oauth_endpoints(
                    tenant_id=server.tenant_id,
                    resource_metadata_url=auth_info.get("resource_metadata"),
                    base_url=server.base_url,
                    db=db
                )

                # Build OAuth discovery response
                oauth_discovery = {
                    "auth_scheme": auth_info["auth_scheme"],
                    "error": auth_info["error"],
                    "error_description": auth_info["error_description"],
                    "resource_metadata": auth_info["resource_metadata"],
                    "realm": auth_info.get("realm"),
                    "scope": auth_info.get("scope")
                }

                # Add discovered information
                if discovered_config:
                    resource_metadata = discovered_config.get("resource_metadata")
                    oidc_config = discovered_config.get("oidc_config")

                    # Add resource metadata to discovery
                    if resource_metadata:
                        oauth_discovery["resource_name"] = resource_metadata.get("resource_name")
                        oauth_discovery["authorization_servers"] = resource_metadata.get("authorization_servers", [])
                        oauth_discovery["scopes_supported"] = resource_metadata.get("scopes_supported", [])

                    # Add OIDC config if available
                    if oidc_config:
                        oauth_discovery.update({
                            "issuer": oidc_config.get("issuer"),
                            "authorization_endpoint": oidc_config.get("authorization_endpoint"),
                            "token_endpoint": oidc_config.get("token_endpoint"),
                            "well_known_url": oidc_config.get("well_known_url")
                        })

                    # Use existing IdP and suggested config from discovery helper
                    existing_idp_info = discovered_config.get("existing_idp")
                    if existing_idp_info:
                        existing_idp = existing_idp_info
                        self.logger.debug("Using existing IdP from discovery", context={
                            "existing_idp_id": existing_idp["id"],
                            "existing_idp_name": existing_idp["name"]
                        })

                    # Only use suggested config if no existing IdP
                    if not existing_idp:
                        suggested_idp = discovered_config.get("suggested_config")

            # Step 2: Handle 401/403 without WWW-Authenticate header
            # Server is reachable but requires auth, even without standard discovery
            if not www_auth_header and connectivity_result.get("http_status") in (401, 403):
                duration_ms = (time.time() - start_time) * 1000
                http_status = connectivity_result["http_status"]

                # Analyze response body for OAuth hints
                oauth_hints = self._extract_oauth_hints_from_body(
                    connectivity_result.get("response_json"),
                    connectivity_result.get("response_body", ""),
                    server.mcp_url
                )

                suggestions = [
                    "✓ Server is reachable and responding",
                    f"⚠ Server returned HTTP {http_status} but did not provide WWW-Authenticate header for OAuth discovery"
                ]

                if oauth_hints.get("mentions_oauth"):
                    suggestions.append(
                        "✓ Server response indicates OAuth 2.0 authentication is required"
                    )
                    if oauth_hints.get("oauth_docs_url"):
                        suggestions.append(
                            f"  - OAuth documentation: {oauth_hints['oauth_docs_url']}"
                        )

                if oauth_hints.get("error_message"):
                    suggestions.append(f"Server message: {oauth_hints['error_message']}")

                suggestions.extend([
                    "⚠ No standard OAuth/OIDC discovery available - manual IdP configuration needed",
                    "You can still create this server and configure a skeleton IdP:",
                    "1. Create/import an IdP with the provider's OAuth endpoints",
                    "2. Register an OAuth client with the provider",
                    "3. Assign the OAuth client to this server"
                ])

                # Build a minimal oauth_discovery indicating auth is required
                # but discovery wasn't possible via standard mechanisms
                oauth_discovery = {
                    "auth_scheme": "Bearer",
                    "error": "invalid_token" if http_status == 401 else "insufficient_scope",
                    "error_description": oauth_hints.get(
                        "error_message",
                        f"Server returned {http_status} without WWW-Authenticate header"
                    ),
                    "resource_metadata": None,
                    "realm": None,
                    "scope": None,
                    "discovery_method": "body_analysis",
                    "oauth_docs_url": oauth_hints.get("oauth_docs_url")
                }

                # Build suggested skeleton IdP config
                hostname = urlparse(server.mcp_url).hostname
                suggested_idp = {
                    "id": f"skeleton-{hostname}",
                    "name": f"OAuth Provider for {hostname}",
                    "type": "skeleton",
                    "issuer": "<oauth server issuer goes here>",
                    "authorization_endpoint": None,
                    "token_endpoint": None,
                    "oidc_discovered": False,
                    "skeleton": True,
                    "notes": "Skeleton IdP - endpoints need manual configuration"
                }

                return {
                    "status": "warning",
                    "connected": False,
                    "error_type": "authentication_required",
                    "error": (
                        f"Server is accessible but requires authentication (HTTP {http_status}). "
                        "No standard OAuth discovery available - manual IdP configuration needed."
                    ),
                    "duration_ms": duration_ms,
                    "http_status": http_status,
                    "content_type": connectivity_result.get("content_type"),
                    "oauth_discovery": oauth_discovery,
                    "suggested_idp_config": suggested_idp,
                    "idp_id": None,
                    "existing_idp": None,
                    "suggestions": suggestions
                }

            # Step 3: If HTTP works, try MCP protocol
            if connectivity_result["connected"]:
                mcp_result = await self._test_mcp_protocol(
                    server=server,
                    timeout=timeout,
                    detailed_response=True,
                    access_token=access_token
                )

                # If MCP succeeds completely, return success
                if mcp_result.get("connected") and mcp_result.get("status") == "success":
                    return mcp_result

                # If we have OAuth discovery, always return warning
                duration_ms = (time.time() - start_time) * 1000

                has_oauth_discovery = oauth_discovery is not None and oauth_discovery.get("auth_scheme")

                if has_oauth_discovery:
                    suggestions = [
                        "✓ Server is reachable and responding",
                        f"✓ OAuth discovery successful - {oauth_discovery['auth_scheme']} authentication required"
                    ]

                    if oauth_discovery.get("error_description"):
                        suggestions.append(f"Server message: {oauth_discovery['error_description']}")

                    if existing_idp:
                        suggestions.append(f"✓ IdP already registered: {existing_idp['name']} ({existing_idp['id']})")
                        suggestions.append("You can create this server now and assign the existing IdP")
                    elif suggested_idp:
                        if suggested_idp.get("oidc_discovered"):
                            suggestions.append("✓ IdP configuration fully discovered via OIDC and ready to import")
                            suggestions.append(
                                f"  - Authorization endpoint: {suggested_idp.get('authorization_endpoint', 'N/A')}")
                            suggestions.append(f"  - Token endpoint: {suggested_idp.get('token_endpoint', 'N/A')}")
                        else:
                            suggestions.append("⚠ IdP partially discovered - manual configuration needed for endpoints")
                        suggestions.append("Import the IdP configuration before assigning it to this server")
                    else:
                        suggestions.append("⚠ IdP configuration could not be discovered")
                        suggestions.append("You can still create this server and configure authentication manually")

                    if oauth_discovery.get("scopes_supported"):
                        scopes_list = ", ".join(oauth_discovery["scopes_supported"][:5])
                        if len(oauth_discovery["scopes_supported"]) > 5:
                            scopes_list += f", ... ({len(oauth_discovery['scopes_supported'])} total)"
                        suggestions.append(f"Required scopes: {scopes_list}")

                    suggestions.extend([
                        "After creating the server, configure OAuth authentication:",
                        "1. Import the discovered IdP (if not already registered)",
                        "2. Register an OAuth client with the provider",
                        "3. Assign the OAuth client to this server"
                    ])

                    # Store idp_id for later use
                    idp_id = None
                    if existing_idp_info:
                        idp_id = existing_idp_info["id"]
                    elif suggested_idp:
                        idp_id = suggested_idp.get("id")

                    return {
                        "status": "warning",
                        "connected": False,
                        "error_type": "authentication_required",
                        "error": "Server is accessible but requires authentication. OAuth discovery successful.",
                        "duration_ms": duration_ms,
                        "http_status": connectivity_result.get("http_status"),
                        "content_type": connectivity_result.get("content_type"),
                        "oauth_discovery": oauth_discovery,
                        "suggested_idp_config": suggested_idp,
                        "idp_id": idp_id,
                        "existing_idp": existing_idp,
                        "suggestions": suggestions
                    }

                # No OAuth discovery - use existing error categorization
                error_info = self._categorize_connection_error(mcp_result.get("error", ""), server)

                enhanced_suggestions = error_info["suggestions"].copy()
                enhanced_suggestions.extend(self._get_service_specific_suggestions(server.mcp_url))

                return {
                    "status": error_info["status"],
                    "connected": False,
                    "error_type": error_info["category"],
                    "error": error_info["message"],
                    "message": error_info["message"],  # UI compatibility
                    "duration_ms": duration_ms,
                    "http_status": connectivity_result.get("http_status"),
                    "content_type": connectivity_result.get("content_type"),
                    "suggestions": enhanced_suggestions
                }

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000

            # Log the full error for debugging
            said = error_text(e, server.mcp_url, "MCP server URL")
            self.logger.error("Connection test failed with exception", context={
                "server_url": server.mcp_url,
                "error": said,
                "error_type": type(e).__name__
            }, exc_info=True)

            return {
                "status": "error",
                "connected": False,
                "error_type": "outbound_policy" if find_refusal(e) is not None else "test_failure",
                "error": said,
                "message": said,  # UI compatibility
                "duration_ms": duration_ms,
                "suggestions": [
                    "Check server logs for more details",
                    f"Error type: {type(e).__name__}"
                ]
            }

    async def _test_basic_http_connectivity(self, url: str, timeout: float) -> dict:
        """Test basic HTTP connectivity and capture www-authenticate"""
        if not httpx:
            return {
                "connected": False,
                "error_type": "httpx_not_available",
                "message": "HTTP client not available",
                "suggestions": ["Install httpx package"]
            }

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)

            # First check DNS resolution
            try:
                socket.gethostbyname(hostname)
            except socket.gaierror:
                return {
                    "connected": False,
                    "error_type": "dns_resolution_failed",
                    "message": f"Cannot resolve hostname '{hostname}'",
                    "suggestions": [
                        "Verify the hostname is correct",
                        "Check your DNS settings",
                        "Try accessing the URL in a web browser"
                    ]
                }

            # Test HTTP connectivity (the outbound policy judges the
            # destination where it connects)
            async with outbound_async_client(timeout=timeout) as client:
                try:
                    response = await client.get(url, follow_redirects=True)

                    # Capture www-authenticate header for OAuth discovery
                    www_auth = response.headers.get("www-authenticate")

                    result = {
                        "connected": True,
                        "http_status": response.status_code,
                        "content_type": response.headers.get("content-type", "unknown"),
                        "server": response.headers.get("server", "unknown"),
                        "www_authenticate": www_auth
                    }

                    # For 401/403 without WWW-Authenticate, capture response body
                    # to detect OAuth hints from the server's error response
                    if response.status_code in (401, 403) and not www_auth:
                        try:
                            body_text = response.text[:2048]  # Limit body size
                            result["response_body"] = body_text

                            # Try to parse as JSON for structured OAuth hints
                            if "json" in response.headers.get("content-type", ""):
                                try:
                                    body_json = json.loads(body_text)
                                    result["response_json"] = body_json
                                except (json.JSONDecodeError, ValueError):
                                    pass
                        except Exception:
                            pass

                    return result

                except OutboundRefused as refused:
                    return {
                        "connected": False,
                        "error_type": "outbound_policy",
                        "message": refusal_message(url, "MCP server URL", refused.reason),
                        "suggestions": [
                            f"An operator allows an internal MCP server by adding its host to {ALLOW_SETTING}",
                        ],
                    }
                except httpx.ConnectError:
                    return {
                        "connected": False,
                        "error_type": "connection_refused",
                        "message": f"Cannot connect to server at {url}",
                        "suggestions": [
                            "Check if the server is running",
                            "Verify the port number is correct",
                            "Check firewall settings",
                            *self._docker_localhost_suggestions(url),
                        ]
                    }
                except httpx.TimeoutException:
                    return {
                        "connected": False,
                        "error_type": "timeout",
                        "message": f"Connection timeout after {timeout} seconds",
                        "suggestions": [
                            "Try increasing the timeout value",
                            "Check if the server is overloaded"
                        ]
                    }
                except httpx.HTTPStatusError as e:
                    # Even HTTP errors mean we can connect
                    www_auth = e.response.headers.get("www-authenticate")

                    return {
                        "connected": True,
                        "http_status": e.response.status_code,
                        "content_type": e.response.headers.get("content-type", "unknown"),
                        "www_authenticate": www_auth,
                        "note": f"HTTP {e.response.status_code} response received"
                    }

        except Exception as e:
            return {
                "connected": False,
                "error_type": "unknown",
                "message": f"Unexpected error: {str(e)}",
                "suggestions": ["Check the URL format and try again"]
            }

    # ── stateless (MCP 2026-07-28) probing ──────────────────────────
    # The pinned python-mcp SDK (1.x) speaks only the legacy handshake
    # dialect, so the modern era is probed with raw httpx. Mirrors the
    # Rust gateway's `discover_capabilities` era detection.

    MCP_MODERN_VERSION = "2026-07-28"
    _META_PV = "io.modelcontextprotocol/protocolVersion"
    _META_CC = "io.modelcontextprotocol/clientCapabilities"
    _META_SI = "io.modelcontextprotocol/serverInfo"

    async def _modern_rpc(
        self,
        url: str,
        method: str,
        extra_headers: Optional[Dict[str, str]],
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        """One stateless 2026-07-28 JSON-RPC call. Returns the parsed
        `result` object, or None on any failure (the caller treats None
        as "not a modern server / call unsupported")."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                "_meta": {self._META_PV: self.MCP_MODERN_VERSION, self._META_CC: {}}
            },
        }
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": self.MCP_MODERN_VERSION,
            "mcp-method": method,
            **(extra_headers or {}),
        }
        async with outbound_async_client(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            return None
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        parsed = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(parsed, dict) and "result" in parsed:
                        return parsed["result"]
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        result = body.get("result")
        return result if isinstance(result, dict) else None

    async def _probe_stateless(
        self,
        server: MCPServer,
        access_token: Optional[str],
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        """`server/discover` era-detection probe. A DiscoverResult means
        the upstream speaks the stateless dialect; any error means the
        legacy handshake should be used instead (the spec's own flow)."""
        extra = {"Authorization": f"Bearer {access_token}"} if access_token else None
        try:
            result = await self._modern_rpc(
                server.mcp_url, "server/discover", extra, timeout
            )
        except Exception:
            return None
        if not result or not isinstance(result.get("supportedVersions"), list):
            return None
        return result

    async def _test_mcp_protocol(
        self,
        server: MCPServer,
        timeout: float,
        detailed_response: bool = False,
        access_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Test MCP protocol initialization"""
        start_time = time.time()

        # A 2026-07-28 stateless server has no `initialize`;
        # the SDK handshake below would FAIL against it. Probe
        # `server/discover` first and answer from that when it works.
        discover = await self._probe_stateless(server, access_token, timeout)
        if discover is not None:
            duration_ms = (time.time() - start_time) * 1000
            si = (discover.get("_meta") or {}).get(self._META_SI) or {}
            result: Dict[str, Any] = {
                "status": "success",
                "connected": True,
                "duration_ms": duration_ms,
                "latency": duration_ms,
                "protocol_version": self.MCP_MODERN_VERSION,
                "dialect": "stateless",
                "server_info": {
                    "name": si.get("name", server.name),
                    "version": si.get("version", "unknown"),
                    "protocol_version": self.MCP_MODERN_VERSION,
                },
            }
            if detailed_response:
                summary = {"tools": 0, "resources": 0, "prompts": 0, "resource_templates": 0}
                extra = (
                    {"Authorization": f"Bearer {access_token}"} if access_token else None
                )
                try:
                    tools = await self._modern_rpc(
                        server.mcp_url, "tools/list", extra, timeout
                    )
                    summary["tools"] = len((tools or {}).get("tools", []))
                except Exception:
                    pass
                result["capabilities_summary"] = summary
            return result

        try:
            # Prepare authentication headers
            headers = {}
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

            # Test MCP connection
            async with streamablehttp_client(
                server.mcp_url,
                headers=headers,
                httpx_client_factory=policy_mcp_http_client,
                timeout=timeout
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await session.initialize()

                    duration_ms = (time.time() - start_time) * 1000

                    negotiated = getattr(
                        init_result, 'protocol_version', None
                    ) or getattr(init_result, 'protocolVersion', '2024-11-05')
                    result = {
                        "status": "success",
                        "connected": True,
                        "duration_ms": duration_ms,
                        "latency": duration_ms,
                        # Surface the negotiated wire contract
                        # so the admin form can show it on test success.
                        "protocol_version": negotiated,
                        "dialect": "handshake",
                        "server_info": {
                            "name": getattr(init_result.serverInfo, 'name', server.name),
                            "version": getattr(init_result.serverInfo, 'version', 'unknown'),
                            "protocol_version": negotiated
                        }
                    }

                    # Add detailed info if requested
                    if detailed_response:
                        capabilities_summary = {
                            "tools": 0,
                            "resources": 0,
                            "prompts": 0,
                            "resource_templates": 0
                        }

                        try:
                            tools_result = await session.list_tools()
                            capabilities_summary["tools"] = len(tools_result.tools)
                        except Exception:
                            pass

                        try:
                            resources_result = await session.list_resources()
                            capabilities_summary["resources"] = len(resources_result.resources)
                        except Exception:
                            pass

                        try:
                            prompts_result = await session.list_prompts()
                            capabilities_summary["prompts"] = len(prompts_result.prompts)
                        except Exception:
                            pass

                        try:
                            templates_result = await session.list_resource_templates()
                            capabilities_summary["resource_templates"] = len(
                                templates_result.resourceTemplates
                            )
                        except Exception:
                            pass

                        result["capabilities_summary"] = capabilities_summary
                        result["session_established"] = True

                    return result

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            refused = find_refusal(e)
            if refused is not None:
                return {
                    "status": "error",
                    "connected": False,
                    "error_type": "outbound_policy",
                    "error": refusal_message(server.mcp_url, "MCP server URL", refused.reason),
                    "duration_ms": duration_ms,
                    "latency": duration_ms,
                    "suggestions": [
                        f"An operator allows an internal MCP server by adding its host to {ALLOW_SETTING}",
                    ],
                }
            error_info = self._categorize_connection_error(str(e), server)

            return {
                "status": error_info["status"],
                "connected": False,
                "error_type": error_info["category"],
                "error": error_info["message"],
                "duration_ms": duration_ms,
                "latency": duration_ms,
                "suggestions": error_info["suggestions"]
            }

    def _categorize_connection_error(
        self, error_message: str, server: MCPServer
    ) -> Dict[str, Any]:
        """Categorize connection errors and provide helpful suggestions"""
        error_lower = error_message.lower()

        if "401" in error_message or "unauthorized" in error_lower:
            return {
                "status": "warning",
                "category": "authentication_required",
                "message": f"Authentication required. Server returned: {error_message}",
                "suggestions": self._get_auth_suggestions(server)
            }
        elif "403" in error_message or "forbidden" in error_lower:
            return {
                "status": "warning",
                "category": "permission_denied",
                "message": f"Access forbidden. Server returned: {error_message}",
                "suggestions": [
                    "Check if your credentials have the required permissions",
                    "Verify your API key or token is still valid",
                    "Contact the service administrator for access"
                ]
            }
        elif "404" in error_message or "not found" in error_lower:
            return {
                "status": "error",
                "category": "endpoint_not_found",
                "message": f"MCP endpoint not found. Server returned: {error_message}",
                "suggestions": [
                    f"Verify the URL '{server.mcp_url}' is correct",
                    "Check if this is a valid MCP protocol endpoint",
                    "Ensure the server is running and accessible"
                ]
            }
        elif "timeout" in error_lower:
            return {
                "status": "error",
                "category": "timeout",
                "message": f"Connection timeout after {server.timeout}s. {error_message}",
                "suggestions": [
                    "Try increasing the timeout value",
                    "Check if the server is responding slowly",
                    "Verify network connectivity"
                ]
            }
        elif "connection" in error_lower or "connect" in error_lower:
            return {
                "status": "error",
                "category": "connection_failed",
                "message": f"Failed to establish connection. {error_message}",
                "suggestions": [
                    "Check if the server is running",
                    "Verify the hostname/IP address is correct",
                    "Check firewall and network settings",
                    *self._docker_localhost_suggestions(server.base_url or ""),
                ]
            }
        elif "dns" in error_lower or "name" in error_lower:
            return {
                "status": "error",
                "category": "dns_resolution_failed",
                "message": f"Could not resolve hostname. {error_message}",
                "suggestions": [
                    "Check if the hostname is spelled correctly",
                    "Verify DNS settings",
                    "Try accessing the URL in a web browser"
                ]
            }
        else:
            return {
                "status": "error",
                "category": "unknown",
                "message": error_message,
                "suggestions": [
                    "Check server logs for more details",
                    "Verify all connection parameters",
                    "Try testing with a simpler MCP client"
                ]
            }

    def _get_auth_suggestions(self, server: MCPServer) -> List[str]:
        """Get authentication-specific suggestions"""
        suggestions = [
            f"Server '{server.mcp_url}' requires authentication",
            "Ensure you have valid credentials for this service"
        ]

        if server.auth_type == "none":
            suggestions.extend([
                "Update server configuration to use 'bearer' or 'oauth' auth_type",
                "Add authentication token to server configuration"
            ])
        elif server.auth_type == "bearer":
            suggestions.extend([
                "Check if the bearer token is valid and not expired",
                "Verify the token has the required permissions/scopes"
            ])
        elif server.auth_type == "oauth":
            suggestions.extend([
                "Complete OAuth authentication flow first",
                "Check if OAuth token has expired and needs refresh"
            ])

        suggestions.extend(self._get_service_specific_suggestions(server.mcp_url))
        return suggestions

    @staticmethod
    def _docker_localhost_suggestions(url: str) -> List[str]:
        """Extra hint when the target is the operator's own machine.

        When the platform runs in Docker, "localhost" resolves to the
        control-plane container itself — not the host where a locally
        started MCP server is listening. This is the single most common
        cause of a failed connection test in the trial bundle, and the
        fix differs per OS, so spell it out.
        """
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return []
        if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
            return []
        return [
            "Running Brutor in Docker and the MCP server on your host machine? "
            "Replace localhost with 'host.docker.internal' — works out of the box "
            "on Mac and Windows (Docker Desktop). On Linux, additionally add "
            '"--add-host=host.docker.internal:host-gateway" to the container '
            "(extra_hosts in docker-compose), or use the Docker bridge IP "
            "172.17.0.1 instead."
        ]

    def _get_service_specific_suggestions(self, url: str) -> List[str]:
        """Get service-specific authentication suggestions"""
        if "githubcopilot.com" in url:
            return [
                "Requires GitHub account with active Copilot subscription",
                "Generate GitHub personal access token with appropriate scopes"
            ]
        elif "openai.com" in url:
            return ["Requires valid OpenAI API key"]
        return []

    def _extract_oauth_hints_from_body(
        self,
        response_json: Optional[Dict[str, Any]],
        response_body: str,
        url: str
    ) -> Dict[str, Any]:
        """
        Analyze HTTP response body for OAuth/authentication hints

        Some servers (e.g. HubSpot) return 401 without WWW-Authenticate
        but include OAuth information in the response body.

        Returns:
            Dict with: mentions_oauth, error_message, oauth_docs_url
        """
        hints: Dict[str, Any] = {
            "mentions_oauth": False,
            "error_message": None,
            "oauth_docs_url": None
        }

        body_lower = response_body.lower() if response_body else ""

        # Check for OAuth mentions in body text
        oauth_keywords = ["oauth", "oauth2", "oauth 2.0", "authorization", "bearer token"]
        if any(kw in body_lower for kw in oauth_keywords):
            hints["mentions_oauth"] = True

        # Extract from JSON response if available
        if response_json and isinstance(response_json, dict):
            # Extract error message
            error_msg = (
                response_json.get("message")
                or response_json.get("detail")
                or response_json.get("error_description")
                or response_json.get("error")
            )
            if error_msg and isinstance(error_msg, str):
                hints["error_message"] = error_msg

            # Look for OAuth docs URL in links/references
            links = response_json.get("links", {})
            if isinstance(links, dict):
                oauth_url = links.get("oauth") or links.get("auth") or links.get("documentation")
                if oauth_url:
                    hints["oauth_docs_url"] = oauth_url
                    hints["mentions_oauth"] = True

            # Check category or type fields
            category = str(response_json.get("category", "")).lower()
            error_type = str(response_json.get("type", "")).lower()
            if "auth" in category or "auth" in error_type:
                hints["mentions_oauth"] = True

        # If body mentions common auth patterns but no JSON was parsed
        if not hints["mentions_oauth"]:
            auth_patterns = [
                "authentication credentials",
                "authentication required",
                "invalid_authentication",
                "access token",
                "api key"
            ]
            if any(pattern in body_lower for pattern in auth_patterns):
                hints["mentions_oauth"] = True

        return hints

    # ========================================================================
    # Capability Discovery
    # ========================================================================

    async def discover_capabilities(
        self,
        server: MCPServer,
        db: Session,
        access_token: Optional[str] = None,
        out_poison_findings: Optional[list] = None,
        out_diff_summary: Optional[dict] = None,
    ) -> bool:
        """Discover server capabilities and update database.

        `out_poison_findings`: optional list the caller can pass in to
        receive THREAT-01 scan results. When the registration-time
        prompt-injection scanner finds tool descriptions matching the
        detector, the resulting `PoisonHit` records are appended to
        this list so the caller (e.g. the Create-MCP-Server endpoint)
        can include them in its synchronous response payload — the
        admin UI uses that to pop up an immediate warning rather than
        waiting for the operator to discover the async system_alert
        on their next visit to Mission Control.

        `out_diff_summary`: optional dict the caller can pass in to
        receive a Sprint-3.1 capability-drift summary keyed
        `is_empty` (bool), `change_count` (int), `summary` (str). The
        Refresh-Capabilities admin action uses this so its toast can
        say "Refreshed: 6 tools (drift detected — 1 description
        changed)" without re-querying the snapshot table. The full
        diff still lives in the `resource_health` system_alert.
        """
        start_time = time.time()

        try:
            # System servers (http://system/*) are synthesized by the Rust
            # gateway — the base_url is a synthetic hostname, so a network
            # probe can only DNS-fail. Their capabilities are maintained
            # internally (seed + SystemServerCapabilitySync); a refresh just
            # re-stamps the wire identity we know by construction
            # and the discovery timestamp.
            if is_system_server(server.base_url):
                from app.constants.system_server import (
                    system_server_wire_identity,
                )
                server.protocol_version, server.dialect = (
                    system_server_wire_identity()
                )
                server.discovered_at = datetime.now(timezone.utc)
                db.flush()
                self.logger.info(
                    "System server — capabilities managed internally; "
                    "wire identity re-stamped",
                    context={
                        "server_id": server.id,
                        "protocol_version": server.protocol_version,
                        "dialect": server.dialect,
                    },
                )
                return True

            # For OAuth servers, skip if no token available
            if server.auth_type == "oauth" and not access_token:
                self.logger.warning(
                    "Skipping capability discovery - OAuth server without token",
                    context={"server_id": server.id}
                )
                return False

            # A server with a static token is called with it, as the
            # gateway calls it. A stored value that cannot be read is not
            # a token: the server is not called at all.
            if not access_token and server.auth_token:
                try:
                    access_token = server_token.open_token(self.encryption_service, server.auth_token)
                except server_token.TokenUnreadable:
                    self.logger.warning(
                        "Skipping capability discovery - the server's stored token cannot be decrypted",
                        context={"server_id": server.id}
                    )
                    return False

            # Era detection: probe `server/discover` first. A
            # stateless 2026-07-28 server has no `initialize`, so the SDK
            # session below can never discover it; a dual-era server
            # answers both, and the modern path is preferred.
            timeout = float(getattr(server, "timeout", None) or 30.0)
            discover = await self._probe_stateless(server, access_token, timeout)
            if discover is not None:
                extra = (
                    {"Authorization": f"Bearer {access_token}"} if access_token else None
                )

                async def _list(method: str, key: str) -> list:
                    try:
                        result = await self._modern_rpc(
                            server.mcp_url, method, extra, timeout
                        )
                        items = (result or {}).get(key, [])
                        return items if isinstance(items, list) else []
                    except Exception:
                        return []

                tools = await _list("tools/list", "tools")
                resources = await _list("resources/list", "resources")
                prompts = await _list("prompts/list", "prompts")
                resource_templates = await _list(
                    "resources/templates/list", "resourceTemplates"
                )

                si = (discover.get("_meta") or {}).get(self._META_SI) or {}
                server_info_dict = {
                    "name": si.get("name", server.name),
                    "version": si.get("version", "unknown"),
                }

                enhanced_capabilities: Dict[str, Any] = {
                    "tools": tools,
                    "resources": resources,
                    "resource_templates": resource_templates,
                    "prompts": prompts,
                }
                # SEP-2549 — honor the advertised freshness window
                # (same `_brutor` marker the Rust gateway writes).
                ttl_ms = discover.get("ttlMs")
                if isinstance(ttl_ms, int):
                    enhanced_capabilities["_brutor"] = {
                        "ttl_ms": ttl_ms,
                        "fetched_epoch": int(time.time()),
                    }
                # SEP-2133 — persist the negotiated extensions map
                # (MCP Apps, Tasks, auth extensions, vendor extensions).
                extensions = (discover.get("capabilities") or {}).get("extensions")
                if isinstance(extensions, dict):
                    enhanced_capabilities["extensions"] = extensions

                enhanced_capabilities = normalize_mcp_capabilities(enhanced_capabilities)
                server.update_capabilities(enhanced_capabilities, server_info_dict)
                # Modern servers have no GET listener channel (405).
                server.wants_listener = False
                server.protocol_version = self.MCP_MODERN_VERSION
                server.dialect = "stateless"
                db.flush()

                # Same drift diff/alert as the legacy path.
                try:
                    from app.services.mcp.capability_diff import (
                        record_and_diff,
                        TRIGGER_DISCOVERY,
                    )
                    diff = record_and_diff(
                        db, server, enhanced_capabilities,
                        trigger=TRIGGER_DISCOVERY,
                    )
                    if out_diff_summary is not None:
                        out_diff_summary["is_empty"] = diff.is_empty
                        out_diff_summary["change_count"] = len(diff.changes)
                        out_diff_summary["summary"] = (
                            diff.summary_message() if not diff.is_empty else ""
                        )
                except Exception as diff_err:
                    self.logger.warning(
                        "Capability diff failed (non-critical)",
                        context={"server_id": server.id, "error": str(diff_err)},
                    )

                self.logger.info(
                    "Capabilities discovered via server/discover (stateless dialect)",
                    context={
                        "server_id": server.id,
                        "tools": len(tools),
                        "resources": len(resources),
                        "prompts": len(prompts),
                    },
                )
                return True

            async with self._create_session_context(
                    server, access_token
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    # Initialize the session
                    init_result = await session.initialize()

                    # Extract server info
                    server_info_dict = {
                        "name": getattr(init_result.serverInfo, 'name', server.name),
                        "version": getattr(init_result.serverInfo, 'version', 'unknown')
                    }

                    # Discover capabilities
                    tools = []
                    resources = []
                    resource_templates = []
                    prompts = []

                    # List tools
                    try:
                        tools_result = await session.list_tools()
                        # Canonical normaliser — keeps annotations, title,
                        # outputSchema, _meta (the runtime gates on them).
                        tools = normalize_tools(tools_result.tools)
                    except Exception as e:
                        self.logger.warning(
                            "Tools discovery failed",
                            context={"server_id": server.id, "error": str(e)}
                        )

                    # List resources
                    try:
                        resources_result = await session.list_resources()
                        resources = [
                            self._serialize_resource(res)
                            for res in resources_result.resources
                        ]
                    except Exception as e:
                        self.logger.warning(
                            "Resources discovery failed",
                            context={"server_id": server.id, "error": str(e)}
                        )

                    # List resource templates
                    try:
                        templates_result = await session.list_resource_templates()
                        resource_templates = [
                            self._serialize_template(tmpl)
                            for tmpl in templates_result.resourceTemplates
                        ]
                    except Exception as e:
                        self.logger.warning(
                            "Resource templates discovery failed",
                            context={"server_id": server.id, "error": str(e)}
                        )

                    # List prompts
                    try:
                        prompts_result = await session.list_prompts()
                        prompts = [
                            self._serialize_prompt(prompt)
                            for prompt in prompts_result.prompts
                        ]
                    except Exception as e:
                        self.logger.warning(
                            "Prompts discovery failed",
                            context={"server_id": server.id, "error": str(e)}
                        )

                    # Derive `wants_listener` from the
                    # `initialize` response capabilities. A
                    # server that declares NONE of
                    # `tools.listChanged`,
                    # `resources.listChanged`/`subscribe`,
                    # `prompts.listChanged`, or `logging` has
                    # nothing to push and the SSE listener
                    # channel is provably useless against it —
                    # the gateway short-circuits listener GETs
                    # to 405 and the portal SDK skips opening
                    # the listener when this flag is False.
                    # Mirrors the Rust derivation in
                    # `oauth.rs::derive_wants_listener` so
                    # admin-triggered re-discovery reaches the
                    # same conclusion the runtime path does.
                    wants_listener = self._derive_wants_listener(
                        getattr(init_result, "capabilities", None)
                    )

                    # Update database record
                    enhanced_capabilities = {
                        "tools": tools,
                        "resources": resources,
                        "resource_templates": resource_templates,
                        "prompts": prompts
                    }

                    enhanced_capabilities = normalize_mcp_capabilities(enhanced_capabilities)
                    server.update_capabilities(enhanced_capabilities, server_info_dict)
                    server.wants_listener = wants_listener
                    # Record the legacy wire contract too.
                    server.protocol_version = (
                        getattr(init_result, "protocol_version", None)
                        or getattr(init_result, "protocolVersion", None)
                        or "2024-11-05"
                    )
                    server.dialect = "handshake"
                    db.flush()

                    # Record a new snapshot, diff it
                    # against the previous one, raise a system_alert
                    # if the tool-set drifted. Defensive try/except
                    # because diff/alert is non-critical to discovery —
                    # an exception here should NOT roll back the
                    # capability update.
                    try:
                        from app.services.mcp.capability_diff import (
                            record_and_diff,
                            TRIGGER_DISCOVERY,
                        )
                        diff = record_and_diff(
                            db, server, enhanced_capabilities,
                            trigger=TRIGGER_DISCOVERY,
                        )
                        if out_diff_summary is not None:
                            out_diff_summary["is_empty"] = diff.is_empty
                            out_diff_summary["change_count"] = len(diff.changes)
                            out_diff_summary["summary"] = (
                                diff.summary_message() if not diff.is_empty else ""
                            )
                        if not diff.is_empty:
                            self.logger.warning(
                                "MCP capability drift detected",
                                context={
                                    "server_id": server.id,
                                    "summary": diff.summary_message(),
                                    "change_count": len(diff.changes),
                                },
                            )
                    except Exception as e:  # noqa: BLE001
                        self.logger.error(
                            "Capability diff/alert failed (continuing)",
                            context={
                                "server_id": server.id,
                                "error": str(e),
                            },
                            exc_info=True,
                        )

                    # THREAT-01 — scan tool descriptions for
                    # prompt-injection payloads. Same defensive
                    # try/except convention as capability_diff: a
                    # scanner exception must not abort the discovery
                    # transaction. Gated inside the scanner by
                    # GuardrailConfig.mcp_registration_enabled +
                    # prompt_injection_enabled, so this call is a
                    # no-op for tenants that haven't opted in.
                    try:
                        from app.services.mcp.tool_description_scanner import (
                            scan_for_poisoned_descriptions,
                        )
                        poison_hits = scan_for_poisoned_descriptions(
                            db, server, enhanced_capabilities,
                        )
                        if poison_hits:
                            self.logger.warning(
                                "MCP tool description poisoning detected",
                                context={
                                    "server_id": server.id,
                                    "poisoned_tool_count": len(poison_hits),
                                    "poisoned_tools": [
                                        h.tool_name for h in poison_hits
                                    ],
                                },
                            )
                            # Surface findings to the caller (e.g.
                            # the Create-MCP-Server endpoint) so it
                            # can return them inline and the admin
                            # UI shows a modal at click time. Async
                            # system_alert rows are already written
                            # by the scanner — this is the
                            # synchronous side-channel.
                            if out_poison_findings is not None:
                                out_poison_findings.extend(poison_hits)
                    except Exception as e:  # noqa: BLE001
                        self.logger.error(
                            "Tool-description poison scan failed (continuing)",
                            context={
                                "server_id": server.id,
                                "error": str(e),
                            },
                            exc_info=True,
                        )

                    duration_ms = (time.time() - start_time) * 1000
                    self.logger.debug("Server capabilities discovered", context={
                        "server_id": server.id,
                        "tools_count": len(tools),
                        "resources_count": len(resources),
                        "prompts_count": len(prompts),
                        "wants_listener": wants_listener,
                        "duration_ms": duration_ms,
                        "used_provided_token": access_token is not None
                    })

                    return True

        except Exception as e:
            self.logger.error(
                "Failed to discover server capabilities",
                context={"server_id": server.id, "error": error_text(e)},
                exc_info=True
            )
            raise

    # ========================================================================
    # Capability Serialization Helpers
    # ========================================================================

    @staticmethod
    def _derive_wants_listener(capabilities) -> bool:
        """Decide whether a server wants its SSE listener channel
        opened, based on the `capabilities` object returned by
        MCP `initialize`.

        Per spec, the listener (long-lived `GET text/event-stream`)
        is OPTIONAL and only useful when the server emits
        notifications. The MCP capability flags that imply
        notifications are:
          * `tools.listChanged`
          * `resources.listChanged` / `resources.subscribe`
          * `prompts.listChanged`
          * `logging` (the presence of the field, with any
            value, signals the server emits log messages)

        Anything else means the server is pure
        request/response and the listener will never carry
        traffic. We then flag the server so the gateway short-
        circuits listener GETs to 405 and the portal SDK skips
        opening the stream — preventing the SSE reconnect
        loops that otherwise pump hundreds of junk req/h
        against non-listener servers.

        Defensive default: `True` when we can't read the
        capabilities (None, malformed, or surprise wire
        format). That keeps existing behaviour for unknown
        servers — the worst case is one wasted GET per
        connect, which the gateway 405s and caches anyway.
        """
        if capabilities is None:
            return True

        # The mcp Python SDK exposes capabilities as a Pydantic
        # model with snake_case attribute names (list_changed,
        # not listChanged). Accessing missing attributes raises
        # AttributeError, so getattr with a default is safer
        # than dot access.
        def _flag(obj, attr: str) -> bool:
            if obj is None:
                return False
            v = getattr(obj, attr, None)
            return bool(v) if v is not None else False

        any_list_changed = (
            _flag(getattr(capabilities, "tools", None), "list_changed")
            or _flag(getattr(capabilities, "resources", None), "list_changed")
            or _flag(getattr(capabilities, "prompts", None), "list_changed")
        )
        resources_subscribe = _flag(getattr(capabilities, "resources", None), "subscribe")
        # `logging` is a marker capability — if the server
        # mentions it at all (even as an empty object) it
        # promises log notifications.
        logging_present = getattr(capabilities, "logging", None) is not None

        return any_list_changed or resources_subscribe or logging_present

    def _serialize_resource(self, resource) -> Dict[str, Any]:
        """Serialize Resource to dict, ensuring all values are JSON-serializable"""
        result = {
            "uri": str(getattr(resource, 'uri', '')),  # Convert AnyUrl to string
            "name": str(getattr(resource, 'name', ''))
        }
        if hasattr(resource, 'description') and resource.description:
            result["description"] = str(resource.description)
        if hasattr(resource, 'mimeType') and resource.mimeType:
            result["mimeType"] = str(resource.mimeType)
        return result

    def _serialize_template(self, template) -> Dict[str, Any]:
        """Serialize ResourceTemplate to dict, ensuring all values are JSON-serializable"""
        result = {
            "uriTemplate": str(getattr(template, 'uriTemplate', '')),  # Convert to string
            "name": str(getattr(template, 'name', ''))
        }
        if hasattr(template, 'description') and template.description:
            result["description"] = str(template.description)
        if hasattr(template, 'mimeType') and template.mimeType:
            result["mimeType"] = str(template.mimeType)
        return result

    def _serialize_prompt(self, prompt) -> Dict[str, Any]:
        """Serialize Prompt to dict, ensuring all values are JSON-serializable"""
        result = {
            "name": str(getattr(prompt, 'name', '')),
            "description": str(getattr(prompt, 'description', '')) if hasattr(prompt,'description') and prompt.description else ''
        }
        if hasattr(prompt, 'arguments') and prompt.arguments:
            result["arguments"] = [
                {
                    "name": str(getattr(arg, 'name', '')),
                    "description": str(getattr(arg, 'description', '')) if hasattr(arg,'description') and arg.description else '',
                    "required": bool(getattr(arg, 'required', False))
                }
                for arg in prompt.arguments
            ]
        return result

    def _make_json_serializable(self, obj: Any) -> Any:
        """
        Recursively convert Pydantic models and special types to JSON-serializable format

        Args:
            obj: Object to convert

        Returns:
            JSON-serializable version of the object
        """
        # Handle None
        if obj is None:
            return None

        # Handle Pydantic AnyUrl and similar types
        if hasattr(obj, '__class__') and obj.__class__.__name__ in ('AnyUrl', 'HttpUrl', 'AnyHttpUrl'):
            return str(obj)

        # Handle dictionaries
        if isinstance(obj, dict):
            return {key: self._make_json_serializable(value) for key, value in obj.items()}

        # Handle lists
        if isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]

        # Handle Pydantic BaseModel
        if hasattr(obj, 'model_dump'):
            # Pydantic v2
            return self._make_json_serializable(obj.model_dump())
        elif hasattr(obj, 'dict'):
            # Pydantic v1
            return self._make_json_serializable(obj.dict())

        # Handle primitives
        if isinstance(obj, (str, int, float, bool)):
            return obj

        # Fallback: convert to string
        return str(obj)

    # ========================================================================
    # Health Monitoring
    # ========================================================================

    async def check_health(
            self,
            server: MCPServer,
            timeout: float,
            db: Session
    ) -> Dict[str, Any]:
        """
        Check server health using simple HTTP connectivity test

        This does NOT attempt MCP protocol connection or OAuth authentication.
        It simply verifies the server is reachable and responding.

        For OAuth-protected servers, a 401 response is considered HEALTHY
        because it proves the server is alive and responding.

        Args:
            server: MCPServer database model
            user_id: User ID (not used for HTTP health checks)
            timeout: Request timeout in seconds
            db: Database session

        Returns:
            Dictionary with health check results
        """
        # System servers (http://system/*) are virtual — always healthy
        if is_system_server(server.base_url):
            server.update_health_status(
                status=ServerStatusType.HEALTHY.value,
                response_time_ms=0.0
            )
            db.flush()

            return {
                "server_id": server.id,
                "server_name": server.name,
                "server_url": server.mcp_url,
                "status": ServerStatusType.HEALTHY.value,
                "connected": True,
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                "health_status": server.health_status,
                "check_duration_ms": 0.0,
                "note": "System virtual server — always healthy"
            }

        start_time = time.time()

        try:
            # Use existing HTTP connectivity test
            connectivity_result = await self._test_basic_http_connectivity(
                server.mcp_url,
                timeout
            )

            duration_ms = (time.time() - start_time) * 1000

            # Determine health based on connectivity
            # Note: 401 (Unauthorized) is considered healthy for OAuth servers
            is_connected = connectivity_result.get("connected", False)
            http_status = connectivity_result.get("http_status")

            # Server is healthy if:
            # 1. Connected successfully (2xx, 3xx responses)
            # 2. OR returns 401 (server alive but requires authentication)
            # 3. OR returns 403 (server alive but access forbidden)
            is_healthy = is_connected or http_status in [401, 403]

            if is_healthy:
                # Server is reachable and responding
                server.update_health_status(
                    status=ServerStatusType.HEALTHY.value,
                    response_time_ms=duration_ms
                )
                db.flush()

                result = {
                    "server_id": server.id,
                    "server_name": server.name,
                    "server_url": server.mcp_url,
                    "status": ServerStatusType.HEALTHY.value,
                    "connected": True,
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                    "health_status": server.health_status,
                    "check_duration_ms": duration_ms,
                    "http_status": http_status
                }

                # Add note for OAuth-protected servers
                if http_status == 401:
                    result["note"] = "Server is healthy (OAuth authentication required)"
                elif http_status == 403:
                    result["note"] = "Server is healthy (access forbidden but responding)"

                return result

            else:
                # Server is not reachable or has error
                error_msg = connectivity_result.get("message", "Server not reachable")
                error_type = connectivity_result.get("error_type", "unknown")

                server.update_health_status(
                    status=ServerStatusType.UNHEALTHY.value,
                    response_time_ms=duration_ms,
                    error_message=error_msg
                )
                db.flush()

                return {
                    "server_id": server.id,
                    "server_name": server.name,
                    "server_url": server.mcp_url,
                    "status": ServerStatusType.UNHEALTHY.value,
                    "connected": False,
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                    "health_status": server.health_status,
                    "check_duration_ms": duration_ms,
                    "http_status": http_status,
                    "error": error_msg,
                    "error_type": error_type
                }

        except Exception as e:
            # Unexpected error during health check
            duration_ms = (time.time() - start_time) * 1000
            error_msg = error_text(e, server.mcp_url, "MCP server URL")

            self.logger.error("Health check failed with exception", context={
                "server_id": server.id,
                "server_url": server.mcp_url,
                "error": error_msg,
                "error_type": type(e).__name__
            }, exc_info=True)

            server.update_health_status(
                status=ServerStatusType.UNHEALTHY.value,
                response_time_ms=duration_ms,
                error_message=error_msg
            )
            db.flush()

            return {
                "server_id": server.id,
                "server_name": server.name,
                "server_url": server.mcp_url,
                "status": "error",
                "connected": False,
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                "health_status": server.health_status,
                "check_duration_ms": duration_ms,
                "error": error_msg,
                "error_type": type(e).__name__
            }


    # ========================================================================
    # Response Building
    # ========================================================================

    def to_response(self, server: MCPServer, idp_name: Optional[str] = None) -> dict:
        """
        Convert MCPServer database model to API response format

        Args:
            server: MCPServer database model
            idp_name: Optional IdP name from JOIN query

        Returns:
            Dictionary matching ServerResponse schema
        """

        # Build base response.
        #
        # IMPORTANT: every field declared on `ServerResponse` (in
        # `app/api/v1/models/admin/mcp_server.py`) MUST appear in this
        # dict. Pydantic response models drop undeclared keys, but
        # missing keys are filled with the field's default (or `None`
        # if no default), which silently hides DB state from API
        # consumers. The v0.7.95 bench report flagged `wants_listener`
        # specifically: a PATCH that *did* update the DB column read
        # back as `null` because this dict didn't carry the field.
        response = {
            "id": server.id,
            "name": server.name,
            "description": server.description,
            "base_url": server.base_url,
            "mcp_endpoint_path": server.mcp_endpoint_path,
            "url": server.mcp_url,
            "auth_type": server.auth_type,
            # Whether a static token is stored. The token is never returned.
            "has_auth_token": bool(server.auth_token),
            "idp_id": server.idp_id,
            "idp_name": idp_name,
            # Whether the gateway would exchange a code for this server
            # (None for a server that is not OAuth-protected).
            "oauth_pairing": self._oauth_pairing(server),
            "timeout": server.timeout,
            "max_concurrent_requests": server.max_concurrent_requests,
            "enabled": server.enabled,
            "capabilities": server.capabilities,
            "discovered_at": server.discovered_at.isoformat() if server.discovered_at else None,
            # Negotiated MCP wire contract, written by the
            # Rust gateway at capability discovery. NULL until probed.
            "protocol_version": server.protocol_version,
            "dialect": server.dialect,
            # Defaults to True for legacy rows that haven't been re-probed
            # (column NULL). Mirrors the defaulting in MCPServer.to_dict so
            # the two paths can't drift.
            "wants_listener": (
                bool(server.wants_listener) if server.wants_listener is not None else True
            ),
            "server_info": server.server_info,
            "health_status": self._build_health_status(server),
            "capabilities_summary": self._build_capabilities_summary(server),
            "icon": server.get_icon_info(),
            # Tenant-wide default Limits/Policies (min-wins with RG). Must be
            # carried here or the ServerResponse strips them on every read.
            "tenant_limits": server.tenant_limits,
            "tenant_governance": server.tenant_governance,
            # Data residency region. Declared on
            # ServerResponse, so it must be named here or every read
            # comes back NULL over a correctly-stamped row.
            "region": server.region,
            # AI Resource version + authorship, and the timestamps
            # that go with them. Same rule as the note at the top of this
            # builder: declared on ServerResponse is not enough, it has to
            # be named here or the Audit Info tab reads "unknown" and "-"
            # over a correctly-stamped row.
            "version": server.version,
            "created_by": server.created_by,
            "updated_by": server.updated_by,
            "created_at": server.created_at.isoformat() if server.created_at else None,
            "updated_at": server.updated_at.isoformat() if server.updated_at else None,
        }

        return response

    @staticmethod
    def _oauth_pairing(server: MCPServer) -> Optional[Dict[str, Any]]:
        from sqlalchemy import inspect as sa_inspect

        from app.services.mcp.server.oauth_pairing import oauth_pairing

        # A row that is in no session (a connection test's temporary
        # server, an object that is no row at all) has no configuration to
        # ask about.
        state = sa_inspect(server, raiseerr=False)
        db = state.session if state is not None else None
        return oauth_pairing(db, server) if db is not None else None

    def _build_health_status(self, server: MCPServer) -> Optional[Dict[str, Any]]:
        """
        Build health status response from database model

        Uses server.get_health_status_dict() which returns the correct format
        for ServerHealthStatus (embedded in server list responses)

        Returns:
            Dict with: status, last_check, last_success, consecutive_failures, error_message
        """
        # Use the server's own method which returns the correct format
        return server.get_health_status_dict()

    def _build_capabilities_summary(self, server: MCPServer) -> Dict[str, Any]:
        """Build capabilities summary from database model"""
        if not server.capabilities:
            return {
                "tools_count": 0,
                "resources_count": 0,
                "resource_templates_count": 0,
                "prompts_count": 0,
                "has_capabilities": False
            }

        return {
            "tools_count": len(server.capabilities.get("tools", [])),
            "resources_count": len(server.capabilities.get("resources", [])),
            "resource_templates_count": len(
                server.capabilities.get("resource_templates", [])
            ),
            "prompts_count": len(server.capabilities.get("prompts", [])),
            "has_capabilities": True
        }

    # ========================================================================
    # Private Helpers
    # ========================================================================

    def _create_session_context(
        self, server: MCPServer, access_token: Optional[str] = None
    ):
        """Create a session context manager for MCP connection"""
        headers = {}
        if access_token:
            # Clean the token (remove any whitespace)
            token_clean = ''.join(access_token.split())
            headers["Authorization"] = f"Bearer {token_clean}"

        return streamablehttp_client(
            server.mcp_url,
            headers=headers,
            timeout=server.timeout,
            httpx_client_factory=policy_mcp_http_client,
        )