# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Health Check Configuration Pydantic Models

API request/response models for health check configuration endpoints.
These models define the structure for API communication about health check settings.
"""

from pydantic import BaseModel, Field, model_validator
from typing import Optional

from app.services import release_scope

# Settings no component reads: the gateway derives health from live traffic,
# and the Admin Console's polling reads `enabled`, `interval_seconds` and
# `pause_when_inactive`, the background model sweep `ai_settings`'
# `failure_threshold` and `interval_seconds`. Not available in this release:
# a value is refused, and a response shows null.
DASHBOARD_UNREAD = ("show_ai_models", "show_mcp_servers")
MCP_UNREAD = ("timeout_seconds", "retry_attempts", "failure_threshold", "recovery_check_interval")
AI_UNREAD = ("timeout_seconds", "retry_attempts", "recovery_check_interval", "test_prompt", "max_tokens")


def _refuse_unread(settings, section, unread):
    for name in unread:
        if getattr(settings, name) is not None:
            raise ValueError(
                release_scope.not_available(f"Health-check setting {section}.{name}", why="nothing reads it")
            )
    return settings


# =============================================================================
# Settings Models - One for each context
# =============================================================================

class DashboardHealthCheckSettings(BaseModel):
    """
    Dashboard health check settings

    Dashboard provides a high-level overview showing both AI Models and MCP Servers.
    Typically uses a faster refresh interval than detailed tabs.

    Attributes:
        enabled: Whether dashboard health checks are enabled
        interval_seconds: How often to refresh dashboard (10-600 seconds)
    """
    enabled: bool = Field(
        default=True,
        description="Enable dashboard health checks"
    )

    interval_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Dashboard refresh interval (10-600 seconds, default: 60s)"
    )

    show_ai_models: Optional[bool] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    show_mcp_servers: Optional[bool] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    @model_validator(mode="after")
    def _in_this_release(self):
        return _refuse_unread(self, "dashboard_settings", DASHBOARD_UNREAD)

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "interval_seconds": 60
            }
        }


class MCPHealthCheckSettings(BaseModel):
    """
    MCP Server health check settings

    Configuration for MCP server health monitoring in the detailed MCP servers tab.

    Attributes:
        enabled: Whether MCP server health checks are enabled
        interval_seconds: Time between health checks (10-3600 seconds)
        pause_when_inactive: Pause checks for inactive servers
    """
    enabled: bool = Field(
        default=True,
        description="Enable automatic health checks for MCP servers"
    )

    interval_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Time between health checks (10-3600 seconds, default: 300s)"
    )

    timeout_seconds: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    retry_attempts: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    failure_threshold: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    pause_when_inactive: bool = Field(
        default=True,
        description="Pause health checks for inactive servers"
    )

    recovery_check_interval: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    @model_validator(mode="after")
    def _in_this_release(self):
        return _refuse_unread(self, "mcp_settings", MCP_UNREAD)

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "interval_seconds": 300,
                "pause_when_inactive": True
            }
        }


class AIHealthCheckSettings(BaseModel):
    """
    AI Model health check settings

    Configuration for AI model health monitoring in the detailed AI models tab.

    Attributes:
        enabled: Whether AI model health checks are enabled
        interval_seconds: Time between health checks (10-3600 seconds)
        failure_threshold: Consecutive failures before marking unhealthy (1-10)
        pause_when_inactive: Pause checks for inactive models
    """
    enabled: bool = Field(
        default=True,
        description="Enable automatic health checks for AI models"
    )

    interval_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Time between health checks (10-3600 seconds, default: 300s)"
    )

    timeout_seconds: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    retry_attempts: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    failure_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Consecutive failures before marking unhealthy (1-10)"
    )

    pause_when_inactive: bool = Field(
        default=True,
        description="Pause health checks for inactive models"
    )

    recovery_check_interval: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    test_prompt: Optional[str] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    max_tokens: Optional[int] = Field(None, description="Not available in this release: always null, and a value is refused (nothing reads it).")

    @model_validator(mode="after")
    def _in_this_release(self):
        return _refuse_unread(self, "ai_settings", AI_UNREAD)

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "interval_seconds": 300,
                "failure_threshold": 3,
                "pause_when_inactive": True
            }
        }


# =============================================================================
# API Request/Response Models
# =============================================================================

class HealthCheckConfigResponse(BaseModel):
    """
    Complete health check configuration response

    Returned by GET /v1/admin/health-check/config endpoint.
    Contains configuration for all three contexts: Dashboard, MCP Servers, AI Models.

    Attributes:
        id: Unique configuration identifier
        tenant_id: Tenant this configuration belongs to
        dashboard_settings: Dashboard health check configuration
        mcp_settings: MCP server health check configuration
        ai_settings: AI model health check configuration
        created_at: When this configuration was created
        updated_at: When this configuration was last updated
        created_by: Admin who created this configuration
        updated_by: Admin who last updated this configuration
    """
    id: str = Field(description="Configuration ID")
    tenant_id: str = Field(description="Tenant ID")

    dashboard_settings: DashboardHealthCheckSettings = Field(
        description="Dashboard health check settings"
    )
    mcp_settings: MCPHealthCheckSettings = Field(
        description="MCP server health check settings"
    )
    ai_settings: AIHealthCheckSettings = Field(
        description="AI model health check settings"
    )

    created_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 timestamp of creation"
    )
    updated_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 timestamp of last update"
    )
    created_by: Optional[str] = Field(
        default=None,
        description="Username of admin who created this"
    )
    updated_by: Optional[str] = Field(
        default=None,
        description="Username of admin who last updated this"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "id": "hc-config-123",
                "tenant_id": "tenant-456",
                "dashboard_settings": {
                    "enabled": True,
                    "interval_seconds": 60
                },
                "mcp_settings": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "pause_when_inactive": True
                },
                "ai_settings": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "failure_threshold": 3,
                    "pause_when_inactive": True
                },
                "created_at": "2025-01-05T10:00:00Z",
                "updated_at": "2025-01-05T12:00:00Z",
                "created_by": "admin@example.com",
                "updated_by": "admin@example.com"
            }
        }


class UpdateHealthCheckConfigRequest(BaseModel):
    """
    Request to update health check configuration

    Used by PUT /v1/admin/health-check/config endpoint.
    All fields are optional - only provided fields will be updated.

    Attributes:
        dashboard_settings: Optional dashboard settings to update
        mcp_settings: Optional MCP settings to update
        ai_settings: Optional AI settings to update

    Example:
        To update only dashboard interval:
        {
            "dashboard_settings": {
                "interval_seconds": 30
            }
        }

        To update multiple contexts:
        {
            "dashboard_settings": {
                "enabled": True,
                "interval_seconds": 45
            },
            "mcp_settings": {
                "interval_seconds": 600
            }
        }
    """
    dashboard_settings: Optional[DashboardHealthCheckSettings] = Field(
        default=None,
        description="Dashboard settings to update (optional)"
    )

    mcp_settings: Optional[MCPHealthCheckSettings] = Field(
        default=None,
        description="MCP server settings to update (optional)"
    )

    ai_settings: Optional[AIHealthCheckSettings] = Field(
        default=None,
        description="AI model settings to update (optional)"
    )

    class Config:
        extra = "forbid"
        json_schema_extra = {
            "example": {
                "dashboard_settings": {
                    "enabled": True,
                    "interval_seconds": 30
                },
                "mcp_settings": {
                    "interval_seconds": 600
                }
            }
        }


# =============================================================================
# Helper Functions
# =============================================================================

def get_default_config() -> dict:
    """
    Get default configuration values for all contexts

    Returns:
        dict: Default configuration with all three settings sections
    """
    return {
        "dashboard_settings": DashboardHealthCheckSettings().dict(),
        "mcp_settings": MCPHealthCheckSettings().dict(),
        "ai_settings": AIHealthCheckSettings().dict()
    }


def validate_config(config: UpdateHealthCheckConfigRequest) -> list[str]:
    """
    Validate health check configuration

    Performs cross-field validation beyond Pydantic's built-in validation.

    Args:
        config: Configuration to validate

    Returns:
        list[str]: List of validation error messages (empty if valid)
    """
    errors = []

    # Dashboard validation
    if config.dashboard_settings:
        if config.dashboard_settings.interval_seconds < 10:
            errors.append("Dashboard interval must be at least 10 seconds")
        if config.dashboard_settings.interval_seconds > 600:
            errors.append("Dashboard interval cannot exceed 600 seconds")

    # MCP validation
    if config.mcp_settings:
        if config.mcp_settings.timeout_seconds > config.mcp_settings.interval_seconds:
            errors.append("MCP timeout cannot exceed interval")
        if config.mcp_settings.recovery_check_interval > config.mcp_settings.interval_seconds:
            errors.append("MCP recovery interval should not exceed regular interval")

    # AI validation
    if config.ai_settings:
        if config.ai_settings.timeout_seconds > config.ai_settings.interval_seconds:
            errors.append("AI timeout cannot exceed interval")
        if config.ai_settings.recovery_check_interval > config.ai_settings.interval_seconds:
            errors.append("AI recovery interval should not exceed regular interval")

    return errors