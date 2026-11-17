# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Dynamic Client Registration Service (RFC 7591)
Client-side implementation of OAuth 2.0 Dynamic Client Registration Protocol.
Registers OAuth clients dynamically with Identity Providers that support the protocol.
"""

from dataclasses import dataclass
from typing import Optional

import httpx

from app.services.outbound_http import outbound_async_client

from app.core.logging_system import BrutorLogger
from app.database.models.idp_server import IdPServer
from app.services.oauth.encryption_service import EncryptionService


@dataclass
class DCRResult:
    """Result of a successful Dynamic Client Registration"""
    client_id: str
    client_secret: Optional[str]
    client_secret_expires_at: int
    registration_client_uri: Optional[str]
    registration_access_token: Optional[str]


class DynamicClientRegistrationService:
    """
    RFC 7591 Dynamic Client Registration - client-side service.

    Registers OAuth clients with Identity Providers that expose a
    registration_endpoint in their OIDC discovery metadata.
    """

    def __init__(self, encryption_service: EncryptionService, logger: BrutorLogger):
        self.encryption_service = encryption_service
        self.logger = logger

    async def register_client(
        self,
        idp: IdPServer,
        redirect_uris: list[str],
        client_name: str,
        token_endpoint_auth_method: str = "client_secret_post",
        grant_types: list[str] | None = None,
        response_types: list[str] | None = None,
        scope: str | None = None,
    ) -> DCRResult:
        """
        Register a client dynamically with the IdP per RFC 7591.

        Args:
            idp: The IdPServer instance with a registration_endpoint
            redirect_uris: List of redirect URIs for the client
            client_name: Human-readable name for the client
            token_endpoint_auth_method: Auth method for the token endpoint
            grant_types: OAuth grant types the client will use
            response_types: OAuth response types the client will use
            scope: Space-separated list of scopes

        Returns:
            DCRResult with the registered client credentials

        Raises:
            ValueError: If IdP doesn't support DCR or metadata is invalid
            RuntimeError: If registration fails with an unexpected status
        """
        if not idp.registration_endpoint:
            raise ValueError("IdP does not support dynamic client registration")

        # Build client metadata per RFC 7591 Section 2
        metadata = {
            "redirect_uris": redirect_uris,
            "client_name": client_name,
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
        }
        if scope:
            metadata["scope"] = scope

        self.logger.debug("Registering client via RFC 7591 DCR", context={
            "idp_id": idp.id,
            "idp_name": idp.name,
            "registration_endpoint": idp.registration_endpoint,
            "client_name": client_name,
        })

        # POST to registration endpoint
        async with outbound_async_client(timeout=30.0) as client:
            response = await client.post(
                idp.registration_endpoint,
                json=metadata,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code in (200, 201):
            data = response.json()

            self.logger.info("DCR registration successful", context={
                "idp_id": idp.id,
                "client_id": data.get("client_id"),
                "has_secret": bool(data.get("client_secret")),
                "has_registration_uri": bool(data.get("registration_client_uri")),
            })

            return DCRResult(
                client_id=data["client_id"],
                client_secret=data.get("client_secret"),
                client_secret_expires_at=data.get("client_secret_expires_at", 0),
                registration_client_uri=data.get("registration_client_uri"),
                registration_access_token=data.get("registration_access_token"),
            )
        elif response.status_code == 400:
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                detail = response.json()
            else:
                detail = {"error": response.text}

            error_msg = detail.get("error_description", detail.get("error", str(detail)))

            self.logger.warning("DCR registration rejected by IdP", context={
                "idp_id": idp.id,
                "status_code": 400,
                "error": error_msg,
            })

            raise ValueError(f"Invalid client metadata: {error_msg}")
        else:
            self.logger.warning("DCR registration failed", context={
                "idp_id": idp.id,
                "status_code": response.status_code,
                "response_body": response.text[:500],
            })

            raise RuntimeError(
                f"DCR failed with status {response.status_code}: {response.text[:500]}"
            )
