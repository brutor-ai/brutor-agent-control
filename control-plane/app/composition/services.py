# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The long-lived service objects of the control plane.

One `Services` value is built per application, before the FastAPI app exists,
and handed to every router factory and startup step. It replaces the local
variables `create_app` used to close over.

Only what Community routers and steps are built from is here. An Enterprise
router that needs a manager of its own (the deployment drivers, the MCP
registry manager) builds it in its own factory, in the extension.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.end_user.services.authentication import EndUserAuthentication
from app.core.logging_system import BrutorLogger
from app.services.config_manager import ConfigManager
from app.services.guardrails.guardrails_service import GuardrailsService
from app.services.llm.model.llm_models_manager import LLModelsManager
from app.services.llm.router.llm_router_service import LLMRouterService
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from app.services.mcp.server.virtual_server_manager import VirtualServerManager
from app.services.oauth.client_registration_service import DynamicClientRegistrationService
from app.services.oauth.encryption_service import EncryptionService
from app.services.portal.server_config_manager import ServerConfigManager
from app.services.proxy_log_service import UnifiedProxyLogService
from app.services.proxy_metrics_service import UnifiedProxyMetricsService
from app.services.usage_limit_enforcer import UsageLimitEnforcer


@dataclass(frozen=True)
class Services:
    """Everything a router factory or startup step may be built from."""

    logger: BrutorLogger
    config_manager: ConfigManager
    guardrails_service: GuardrailsService
    mcp_server_manager: MCPServerManager
    virtual_server_manager: VirtualServerManager
    server_config_manager: ServerConfigManager
    proxy_logs_service: UnifiedProxyLogService
    proxy_metrics_service: UnifiedProxyMetricsService
    usage_limit_enforcer: UsageLimitEnforcer
    encryption_service: EncryptionService
    end_user_auth: EndUserAuthentication
    llm_server_manager: LLModelsManager
    llm_router_service: LLMRouterService
    dcr_service: DynamicClientRegistrationService


def build_services(logger: BrutorLogger) -> Services:
    """Build the services in the order `create_app` always built them.

    Synchronous and free of database access: the gateway configuration comes
    from environment variables, and the database is initialised later, in the
    application lifespan.
    """
    config_manager = ConfigManager(logger=logger)

    guardrails_service = GuardrailsService(logger=logger)
    virtual_server_manager = VirtualServerManager(logger)
    server_config_manager = ServerConfigManager(logger)
    proxy_logs_service = UnifiedProxyLogService(logger)
    proxy_metrics_service = UnifiedProxyMetricsService(logger)
    usage_limit_enforcer = UsageLimitEnforcer(logger=logger)
    encryption_service = EncryptionService(config_manager=config_manager, logger=logger)
    mcp_server_manager = MCPServerManager(logger, encryption_service=encryption_service)
    end_user_auth = EndUserAuthentication(logger=logger)
    llm_server_manager = LLModelsManager(logger=logger, encryption_service=encryption_service)
    llm_router_service = LLMRouterService(logger=logger)
    dcr_service = DynamicClientRegistrationService(
        encryption_service=encryption_service, logger=logger
    )

    return Services(
        logger=logger,
        config_manager=config_manager,
        guardrails_service=guardrails_service,
        mcp_server_manager=mcp_server_manager,
        virtual_server_manager=virtual_server_manager,
        server_config_manager=server_config_manager,
        proxy_logs_service=proxy_logs_service,
        proxy_metrics_service=proxy_metrics_service,
        usage_limit_enforcer=usage_limit_enforcer,
        encryption_service=encryption_service,
        end_user_auth=end_user_auth,
        llm_server_manager=llm_server_manager,
        llm_router_service=llm_router_service,
        dcr_service=dcr_service,
    )
