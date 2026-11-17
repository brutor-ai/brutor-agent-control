# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OAuth Discovery helper 
Consolidates and improves OAuth/OIDC discovery with proper issuer-based IdP matching
"""

import re
import httpx
from app.services.outbound_http import outbound_async_client
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from sqlalchemy.orm import Session

from app.composition.identity import IDENTITY_PROTOCOLS
from app.database.models.idp_server import IdPServer
from app.constants.oauth import GrantType


class OAuthDiscoveryHelper:
    """Helper class for OAuth discovery from www-authenticate headers"""

    def __init__(self, logger):
        self.logger = logger

    def parse_www_authenticate(self, www_auth_header: str) -> Dict[str, Any]:
        """
        Parse www-authenticate header to extract OAuth information
        """
        result = {
            "auth_scheme": None,
            "error": None,
            "error_description": None,
            "resource_metadata": None,
            "realm": None,
            "scope": None
        }

        try:
            parts = www_auth_header.split(None, 1)
            if len(parts) >= 1:
                result["auth_scheme"] = parts[0]

            if len(parts) >= 2:
                params_str = parts[1]
                pattern = r'(\w+)="([^"]*)"'
                matches = re.findall(pattern, params_str)

                for key, value in matches:
                    if key in result:
                        result[key] = value

            return result

        except Exception as e:
            self.logger.warning(f"Failed to parse www-authenticate header: {e}")
            return result

    async def fetch_resource_metadata(
            self,
            resource_metadata_url: str
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch OAuth resource metadata from RFC 8707 endpoint
        """
        try:
            async with outbound_async_client(timeout=10.0) as client:
                response = await client.get(resource_metadata_url)
                response.raise_for_status()

                metadata = response.json()

                self.logger.debug("Resource metadata fetched successfully", context={
                    "url": resource_metadata_url,
                    "resource_name": metadata.get("resource_name"),
                    "authorization_servers": metadata.get("authorization_servers", [])
                })

                return metadata

        except Exception as e:
            self.logger.warning(f"Failed to fetch resource metadata from {resource_metadata_url}: {e}")
            return None

    async def discover_oauth_endpoints(
            self,
            tenant_id: str,
            resource_metadata_url: Optional[str],
            base_url: str,
            db: Session
    ) -> Optional[Dict[str, Any]]:
        """
        Discover OAuth endpoints and check for existing IdP by issuer

        Returns:
        {
            "resource_metadata": {...},
            "oidc_config": {...},
            "existing_idp": {...} or None,
            "suggested_config": {...} or None
        }
        """
        discovery_result = {
            "resource_metadata": None,
            "oidc_config": None,
            "existing_idp": None,
            "suggested_config": None
        }

        issuer = None

        # Step 1: Try to fetch resource metadata (RFC 8707)
        if resource_metadata_url:
            resource_metadata = await self.fetch_resource_metadata(resource_metadata_url)
            if resource_metadata:
                discovery_result["resource_metadata"] = resource_metadata

                # Extract first authorization server as issuer
                auth_servers = resource_metadata.get("authorization_servers", [])
                if auth_servers:
                    issuer = auth_servers[0]

                    # Try OIDC discovery using the issuer URL
                    oidc_config = await self._try_oidc_discovery_with_issuer(issuer)
                    if oidc_config:
                        discovery_result["oidc_config"] = oidc_config
                        # Use issuer from OIDC config if available (more authoritative)
                        issuer = oidc_config.get("issuer", issuer)

        # Step 2: Fallback to standard OIDC discovery on base URL
        if not issuer:
            try:
                parsed = urlparse(base_url)
                issuer = f"{parsed.scheme}://{parsed.netloc}"

                # Try OIDC discovery with the base URL as issuer
                oidc_config = await self._try_oidc_discovery_with_issuer(issuer)
                if oidc_config:
                    discovery_result["oidc_config"] = oidc_config
                    issuer = oidc_config.get("issuer", issuer)
            except Exception:
                pass

        # Step 3: Check for existing IdP by issuer (case-insensitive)
        if issuer:
            existing_idp = self.check_existing_idp_by_issuer(tenant_id=tenant_id, issuer=issuer, db=db)
            if existing_idp:
                self.logger.debug("Found existing IdP with matching issuer", context={
                    "issuer": issuer,
                    "existing_idp_id": existing_idp.id,
                    "existing_idp_name": existing_idp.name
                })
                discovery_result["existing_idp"] = {
                    "id": existing_idp.id,
                    "name": existing_idp.name,
                    "enabled": existing_idp.enabled,
                    "issuer": existing_idp.issuer
                }
            else:
                # Step 4: Generate suggested config only if no existing IdP
                discovery_result["suggested_config"] = self._generate_suggested_idp_config(
                    issuer,
                    discovery_result.get("oidc_config"),
                    discovery_result.get("resource_metadata")
                )

        return discovery_result if any(discovery_result.values()) else None

    async def _try_oidc_discovery_with_issuer(self, issuer: str) -> Optional[Dict[str, Any]]:
        """
        Try OIDC discovery using issuer URL

        Properly constructs well-known URL from issuer
        According to OIDC spec, discovery URL should be:
        {issuer}/.well-known/openid-configuration

        If issuer is already a full URL with path, we need to handle it correctly.
        """
        try:
            # Normalize issuer - remove trailing slash
            issuer_normalized = issuer.rstrip('/')

            # Construct well-known URL
            well_known_url = f"{issuer_normalized}/.well-known/openid-configuration"

            self.logger.debug(f"Trying OIDC discovery at: {well_known_url}")

            return await self._fetch_oidc_config(well_known_url)
        except Exception as e:
            self.logger.debug(f"OIDC discovery failed for issuer {issuer}: {e}")
            return None

    async def _fetch_oidc_config(self, well_known_url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch OIDC configuration from well-known URL

        According to OIDC Discovery spec (OpenID Connect Discovery 1.0):
        The URL should be: {issuer}/.well-known/openid-configuration
        """
        self.logger.debug(f"Fetching OIDC config from: {well_known_url}")

        try:
            async with outbound_async_client(timeout=10.0) as client:
                response = await client.get(well_known_url)
                response.raise_for_status()
                config = response.json()

                self.logger.debug("OIDC discovery successful", context={
                    "well_known_url": well_known_url,
                    "issuer": config.get("issuer"),
                    "endpoints_found": {
                        "authorization": bool(config.get("authorization_endpoint")),
                        "token": bool(config.get("token_endpoint")),
                        "jwks": bool(config.get("jwks_uri")),
                        "userinfo": bool(config.get("userinfo_endpoint"))
                    }
                })

                return {
                    "issuer": config.get("issuer"),
                    "authorization_endpoint": config.get("authorization_endpoint"),
                    "token_endpoint": config.get("token_endpoint"),
                    "jwks_uri": config.get("jwks_uri"),
                    "userinfo_endpoint": config.get("userinfo_endpoint"),
                    "revocation_endpoint": config.get("revocation_endpoint"),
                    "introspection_endpoint": config.get("introspection_endpoint"),
                    "registration_endpoint": config.get("registration_endpoint"),
                    "scopes_supported": config.get("scopes_supported", []),
                    "response_types_supported": config.get("response_types_supported", []),
                    "grant_types_supported": config.get("grant_types_supported", []),
                    "token_endpoint_auth_methods_supported": config.get("token_endpoint_auth_methods_supported", []),
                    "code_challenge_methods_supported": config.get("code_challenge_methods_supported", []),
                    "well_known_url": well_known_url
                }
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self.logger.debug(f"OIDC discovery endpoint not found at {well_known_url}")
            else:
                self.logger.debug(f"OIDC discovery failed for {well_known_url}: HTTP {e.response.status_code}")
            return None
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
            # Network/connection errors
            self.logger.debug(f"OIDC discovery failed for {well_known_url}: Connection error - {type(e).__name__}")
            return None
        except Exception as e:
            # Catch any other exceptions (JSON parsing, etc.)
            self.logger.debug(f"OIDC discovery failed for {well_known_url}: {type(e).__name__}: {str(e)}")
            return None

    def check_existing_idp_by_issuer(
            self,
            tenant_id: str,
            issuer: str,
            db: Session
    ) -> Optional[IdPServer]:
        """
        Check if IdP already exists by issuer URL (case-insensitive)

        This is the primary method for checking existing IdPs for a tenant.
        """
        # Only an OAuth provider is an existing IdP for an OAuth flow: a row
        # of another identity protocol with the same issuer is not reused.
        oauth_protocols = IDENTITY_PROTOCOLS.oauth_names()

        # Exact case-insensitive match
        existing = db.query(IdPServer).filter(
            IdPServer.tenant_id == tenant_id,
            IdPServer.protocol.in_(oauth_protocols),
            IdPServer.issuer.ilike(issuer)
        ).first()

        if existing:
            return existing

        # Try matching with/without trailing slash
        issuer_normalized = issuer.rstrip('/')
        existing = db.query(IdPServer).filter(
            IdPServer.tenant_id == tenant_id,
            IdPServer.protocol.in_(oauth_protocols),
            IdPServer.issuer.ilike(f"{issuer_normalized}%")
        ).first()

        return existing

    def _generate_suggested_idp_config(
            self,
            issuer: str,
            oidc_config: Optional[Dict[str, Any]] = None,
            resource_metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generate suggested IdP configuration from discovery results"""

        # Generate clean ID from issuer
        parsed = urlparse(issuer)
        domain = parsed.netloc or parsed.path
        idp_id = domain.replace('.', '_').replace(':', '_').replace('-', '_').lower()

        # Extract name from domain
        idp_name = domain.split('.')[0].title() if '.' in domain else domain.title()

        # Determine if full OIDC discovery was successful
        has_full_config = oidc_config and oidc_config.get("authorization_endpoint") and oidc_config.get("token_endpoint")

        # Extract scopes from either resource metadata or OIDC config
        scopes = []
        if resource_metadata and resource_metadata.get("scopes_supported"):
            scopes = resource_metadata["scopes_supported"]
        elif oidc_config and oidc_config.get("scopes_supported"):
            scopes = oidc_config["scopes_supported"]

        config = {
            "id": idp_id,
            "name": f"{idp_name} OAuth",
            "issuer": issuer,
            "description": f"Auto-discovered OAuth provider",
            "enabled": has_full_config,  # Only enable if full config available
            "verified": False,
            "oidc_discovered": bool(oidc_config)
        }

        if oidc_config:
            # Full OIDC discovery successful
            config.update({
                "type": "oidc_discoverable",
                "well_known_url": oidc_config.get("well_known_url"),
                "authorization_endpoint": oidc_config.get("authorization_endpoint"),
                "token_endpoint": oidc_config.get("token_endpoint"),
                "jwks_uri": oidc_config.get("jwks_uri"),
                "userinfo_endpoint": oidc_config.get("userinfo_endpoint"),
                "revocation_endpoint": oidc_config.get("revocation_endpoint"),
                "introspection_endpoint": oidc_config.get("introspection_endpoint"),
                "registration_endpoint": oidc_config.get("registration_endpoint"),
                "supports_dynamic_registration": bool(oidc_config.get("registration_endpoint")),
                "scopes_supported": scopes or oidc_config.get("scopes_supported", []),
                "response_types_supported": oidc_config.get("response_types_supported", ["code"]),
                "grant_types_supported": oidc_config.get("grant_types_supported", [GrantType.AUTHORIZATION_CODE.value]),
                "token_endpoint_auth_methods": oidc_config.get("token_endpoint_auth_methods_supported", ["client_secret_post"]),
                "supports_pkce": "S256" in oidc_config.get("code_challenge_methods_supported", []),
                "supports_refresh_tokens": GrantType.REFRESH_TOKEN.value in oidc_config.get("grant_types_supported", []),
                "supports_client_credentials": "client_credentials" in oidc_config.get("grant_types_supported", [])
            })
        else:
            # Partial discovery only - manual configuration needed
            config.update({
                "type": "manual",
                "supports_dynamic_registration": False,
                "scopes_supported": scopes,
                "response_types_supported": ["code"],
                "grant_types_supported": [GrantType.AUTHORIZATION_CODE.value],
                "token_endpoint_auth_methods": ["client_secret_post"],
                "supports_pkce": True,  # Assume support
                "supports_refresh_tokens": True,
                "supports_client_credentials": False
            })

        return config