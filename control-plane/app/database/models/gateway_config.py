# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Gateway Health Check Configuration Models

Tenant-aware health check settings for Dashboard, MCP Servers, and AI Models.
Each tenant can independently configure health check behavior for each context.
"""

from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, Index, CheckConstraint
from sqlalchemy.sql import func
from datetime import datetime, timezone
from app.database import Base
import uuid


class HealthCheckConfig(Base):
    """
    Tenant-specific health check configuration

    This model stores health check settings for three different contexts:
    1. Dashboard - Overview page that checks both AI Models and MCP Servers
    2. MCP Servers - Detailed MCP server management tab
    3. AI Models - Detailed AI model management tab

    Each context has independent configuration allowing tenants to:
    - Set different check intervals per context
    - Enable/disable checks per context
    - Configure context-specific behavior

    Example:
        - Dashboard: Fast refresh (60s) for overview
        - MCP Servers: Standard checks (300s) for detailed monitoring
        - AI Models: Standard checks (300s) for detailed monitoring
    """
    __tablename__ = "health_check_configs"

    # =========================================================================
    # Primary Key & Tenant Association
    # =========================================================================

    id = Column(
        String(255),
        primary_key=True,
        default=lambda: f"hc-config-{uuid.uuid4()}",
        comment="Unique identifier for this configuration"
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this configuration belongs to"
    )

    # =========================================================================
    # Dashboard Health Check Settings
    # =========================================================================

    dashboard_enabled = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Enable automatic health checks on dashboard"
    )

    dashboard_interval_seconds = Column(
        Integer,
        default=60,
        nullable=False,
        comment="Dashboard refresh interval in seconds (default: 60s for fast overview)"
    )

    dashboard_show_ai_models = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Display AI model health status on dashboard"
    )

    dashboard_show_mcp_servers = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Display MCP server health status on dashboard"
    )

    # =========================================================================
    # MCP Server Health Check Settings
    # =========================================================================

    mcp_enabled = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Enable automatic health checks for MCP servers"
    )

    mcp_interval_seconds = Column(
        Integer,
        default=300,
        nullable=False,
        comment="Interval between MCP server health checks in seconds (default: 300s)"
    )

    mcp_timeout_seconds = Column(
        Integer,
        default=10,
        nullable=False,
        comment="Timeout for MCP server health check requests in seconds"
    )

    mcp_retry_attempts = Column(
        Integer,
        default=3,
        nullable=False,
        comment="Number of retry attempts on health check failure"
    )

    mcp_failure_threshold = Column(
        Integer,
        default=3,
        nullable=False,
        comment="Consecutive failures required before marking server as unhealthy"
    )

    mcp_pause_when_inactive = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Pause health checks when MCP servers are marked as inactive"
    )

    mcp_recovery_check_interval = Column(
        Integer,
        default=60,
        nullable=False,
        comment="Check interval for unhealthy MCP servers in seconds (faster recovery checks)"
    )

    # =========================================================================
    # AI Model Health Check Settings
    # =========================================================================

    ai_enabled = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Enable automatic health checks for AI models"
    )

    ai_interval_seconds = Column(
        Integer,
        default=300,
        nullable=False,
        comment="Interval between AI model health checks in seconds (default: 300s)"
    )

    ai_timeout_seconds = Column(
        Integer,
        default=15,
        nullable=False,
        comment="Timeout for AI model health check requests in seconds"
    )

    ai_retry_attempts = Column(
        Integer,
        default=3,
        nullable=False,
        comment="Number of retry attempts on health check failure"
    )

    ai_failure_threshold = Column(
        Integer,
        default=3,
        nullable=False,
        comment="Consecutive failures required before marking model as unhealthy"
    )

    ai_pause_when_inactive = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Pause health checks when AI models are marked as inactive"
    )

    ai_recovery_check_interval = Column(
        Integer,
        default=60,
        nullable=False,
        comment="Check interval for unhealthy AI models in seconds (faster recovery checks)"
    )

    ai_test_prompt = Column(
        String(500),
        default="ping",
        nullable=False,
        comment="Test prompt used for AI model health check requests"
    )

    ai_max_tokens = Column(
        Integer,
        default=10,
        nullable=False,
        comment="Maximum tokens to generate during health check test requests"
    )

    # =========================================================================
    # Metadata & Audit Fields
    # =========================================================================

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when this configuration was created"
    )

    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        comment="Timestamp when this configuration was last updated"
    )

    created_by = Column(
        String(50),
        comment="Username of admin who created this configuration"
    )

    updated_by = Column(
        String(50),
        comment="Username of admin who last updated this configuration"
    )

    # =========================================================================
    # Table Constraints & Indexes
    # =========================================================================

    __table_args__ = (
        # Unique constraint - one configuration per tenant
        Index('ix_health_check_configs_tenant', 'tenant_id', unique=True),

        # Dashboard validation constraints
        CheckConstraint(
            'dashboard_interval_seconds > 0',
            name='ck_dashboard_interval_positive'
        ),

        # MCP Server validation constraints
        CheckConstraint(
            'mcp_interval_seconds > 0',
            name='ck_mcp_interval_positive'
        ),
        CheckConstraint(
            'mcp_timeout_seconds > 0',
            name='ck_mcp_timeout_positive'
        ),
        CheckConstraint(
            'mcp_retry_attempts >= 0',
            name='ck_mcp_retry_non_negative'
        ),
        CheckConstraint(
            'mcp_failure_threshold > 0',
            name='ck_mcp_threshold_positive'
        ),
        CheckConstraint(
            'mcp_recovery_check_interval > 0',
            name='ck_mcp_recovery_positive'
        ),

        # AI Model validation constraints
        CheckConstraint(
            'ai_interval_seconds > 0',
            name='ck_ai_interval_positive'
        ),
        CheckConstraint(
            'ai_timeout_seconds > 0',
            name='ck_ai_timeout_positive'
        ),
        CheckConstraint(
            'ai_retry_attempts >= 0',
            name='ck_ai_retry_non_negative'
        ),
        CheckConstraint(
            'ai_failure_threshold > 0',
            name='ck_ai_threshold_positive'
        ),
        CheckConstraint(
            'ai_recovery_check_interval > 0',
            name='ck_ai_recovery_positive'
        ),
        CheckConstraint(
            'ai_max_tokens > 0',
            name='ck_ai_max_tokens_positive'
        ),

        {'comment': 'Health check configuration per tenant (Dashboard, MCP Servers, AI Models)'}
    )

    # =========================================================================
    # Instance Methods
    # =========================================================================

    def __repr__(self) -> str:
        """String representation of this configuration"""
        return (
            f"<HealthCheckConfig("
            f"id='{self.id}', "
            f"tenant_id='{self.tenant_id}', "
            f"dashboard={self.dashboard_enabled}, "
            f"mcp={self.mcp_enabled}, "
            f"ai={self.ai_enabled}"
            f")>"
        )

    def to_dict(self) -> dict:
        """
        Convert configuration to dictionary representation

        Returns a structured dictionary with three main sections:
        - dashboard_settings: Dashboard-specific configuration
        - mcp_settings: MCP server configuration
        - ai_settings: AI model configuration

        Returns:
            dict: Complete configuration as nested dictionary
        """
        return {
            'id': self.id,
            'tenant_id': self.tenant_id,
            # The settings no component reads are not shown
            # (`app/api/v1/models/admin/gateway_config.py`, *_UNREAD).
            'dashboard_settings': {
                'enabled': self.dashboard_enabled,
                'interval_seconds': self.dashboard_interval_seconds,
            },
            'mcp_settings': {
                'enabled': self.mcp_enabled,
                'interval_seconds': self.mcp_interval_seconds,
                'pause_when_inactive': self.mcp_pause_when_inactive,
            },
            'ai_settings': {
                'enabled': self.ai_enabled,
                'interval_seconds': self.ai_interval_seconds,
                'failure_threshold': self.ai_failure_threshold,
                'pause_when_inactive': self.ai_pause_when_inactive,
            },
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_by': self.created_by,
            'updated_by': self.updated_by
        }

    # =========================================================================
    # Class Methods
    # =========================================================================

    @staticmethod
    def get_default_config() -> dict:
        """
        Get default configuration values

        These defaults are used when creating a new tenant's health check
        configuration or when values are not explicitly provided.

        Defaults:
        - Dashboard: 60s interval (fast refresh for overview)
        - MCP Servers: 300s interval (5 minutes)
        - AI Models: 300s interval (5 minutes)

        Returns:
            dict: Default configuration values
        """
        return {
            # Dashboard defaults
            'dashboard_enabled': True,
            'dashboard_interval_seconds': 60,
            'dashboard_show_ai_models': True,
            'dashboard_show_mcp_servers': True,

            # MCP Server defaults
            'mcp_enabled': True,
            'mcp_interval_seconds': 300,
            'mcp_timeout_seconds': 10,
            'mcp_retry_attempts': 3,
            'mcp_failure_threshold': 3,
            'mcp_pause_when_inactive': True,
            'mcp_recovery_check_interval': 60,

            # AI Model defaults
            'ai_enabled': True,
            'ai_interval_seconds': 300,
            'ai_timeout_seconds': 15,
            'ai_retry_attempts': 3,
            'ai_failure_threshold': 3,
            'ai_pause_when_inactive': True,
            'ai_recovery_check_interval': 60,
            'ai_test_prompt': 'ping',
            'ai_max_tokens': 10
        }

    def update_from_dict(self, data: dict, updated_by: str = None) -> None:
        """
        Update configuration from dictionary

        Accepts a dictionary with three optional sections:
        - dashboard_settings
        - mcp_settings
        - ai_settings

        Only provided fields are updated; missing fields retain their current values.

        Args:
            data: Dictionary containing settings to update
            updated_by: Username of admin performing the update

        Example:
            config.update_from_dict({
                'dashboard_settings': {
                    'enabled': True,
                    'interval_seconds': 30
                },
                'mcp_settings': {
                    'interval_seconds': 600
                }
            }, updated_by='admin@example.com')
        """
        # Update Dashboard settings
        if 'dashboard_settings' in data:
            dash = data['dashboard_settings']
            if 'enabled' in dash:
                self.dashboard_enabled = dash['enabled']
            if 'interval_seconds' in dash:
                self.dashboard_interval_seconds = dash['interval_seconds']
            if 'show_ai_models' in dash:
                self.dashboard_show_ai_models = dash['show_ai_models']
            if 'show_mcp_servers' in dash:
                self.dashboard_show_mcp_servers = dash['show_mcp_servers']

        # Update MCP Server settings
        if 'mcp_settings' in data:
            mcp = data['mcp_settings']
            if 'enabled' in mcp:
                self.mcp_enabled = mcp['enabled']
            if 'interval_seconds' in mcp:
                self.mcp_interval_seconds = mcp['interval_seconds']
            if 'timeout_seconds' in mcp:
                self.mcp_timeout_seconds = mcp['timeout_seconds']
            if 'retry_attempts' in mcp:
                self.mcp_retry_attempts = mcp['retry_attempts']
            if 'failure_threshold' in mcp:
                self.mcp_failure_threshold = mcp['failure_threshold']
            if 'pause_when_inactive' in mcp:
                self.mcp_pause_when_inactive = mcp['pause_when_inactive']
            if 'recovery_check_interval' in mcp:
                self.mcp_recovery_check_interval = mcp['recovery_check_interval']

        # Update AI Model settings
        if 'ai_settings' in data:
            ai = data['ai_settings']
            if 'enabled' in ai:
                self.ai_enabled = ai['enabled']
            if 'interval_seconds' in ai:
                self.ai_interval_seconds = ai['interval_seconds']
            if 'timeout_seconds' in ai:
                self.ai_timeout_seconds = ai['timeout_seconds']
            if 'retry_attempts' in ai:
                self.ai_retry_attempts = ai['retry_attempts']
            if 'failure_threshold' in ai:
                self.ai_failure_threshold = ai['failure_threshold']
            if 'pause_when_inactive' in ai:
                self.ai_pause_when_inactive = ai['pause_when_inactive']
            if 'recovery_check_interval' in ai:
                self.ai_recovery_check_interval = ai['recovery_check_interval']
            if 'test_prompt' in ai:
                self.ai_test_prompt = ai['test_prompt']
            if 'max_tokens' in ai:
                self.ai_max_tokens = ai['max_tokens']

        # Update metadata
        if updated_by:
            self.updated_by = updated_by
        self.updated_at = datetime.now(timezone.utc)

    def validate(self) -> list[str]:
        """
        Validate configuration values

        Performs business logic validation beyond database constraints.

        Returns:
            list[str]: List of validation error messages (empty if valid)
        """
        errors = []

        # Dashboard validation
        if self.dashboard_interval_seconds < 10:
            errors.append("Dashboard interval must be at least 10 seconds")
        if self.dashboard_interval_seconds > 600:
            errors.append("Dashboard interval cannot exceed 600 seconds (10 minutes)")

        # MCP validation
        if self.mcp_interval_seconds < 10:
            errors.append("MCP interval must be at least 10 seconds")
        if self.mcp_interval_seconds > 3600:
            errors.append("MCP interval cannot exceed 3600 seconds (1 hour)")
        if self.mcp_timeout_seconds > self.mcp_interval_seconds:
            errors.append("MCP timeout cannot exceed interval")

        # AI validation
        if self.ai_interval_seconds < 10:
            errors.append("AI interval must be at least 10 seconds")
        if self.ai_interval_seconds > 3600:
            errors.append("AI interval cannot exceed 3600 seconds (1 hour)")
        if self.ai_timeout_seconds > self.ai_interval_seconds:
            errors.append("AI timeout cannot exceed interval")
        if len(self.ai_test_prompt) > 500:
            errors.append("AI test prompt cannot exceed 500 characters")

        return errors