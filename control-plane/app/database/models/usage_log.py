# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Usage Log Model
Records every interaction through the AI Gateway to track quotas and costs.
Implements hierarchical attribution for roll-up reporting.
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Float, Index, Text, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from typing import Dict, Any, List
from app.database import Base


class UsageLog(Base):
    """
    Usage Log Model - The Gateway's Transaction Ledger

    Records every API request through the gateway with full context for:
    - Quota enforcement at the target group level
    - Hierarchical cost roll-up for parent groups
    - Audit trail and compliance
    - Usage analytics and reporting
    """
    __tablename__ = "usage_logs"

    # ==================== Primary Key ====================
    id = Column(Integer, primary_key=True, autoincrement=True)

    # ==================== Multi-tenancy ====================
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    # ==================== Identity: WHO ====================
    user_id = Column(
        Integer,
        ForeignKey("end_users.id", ondelete="SET NULL"),
        nullable=True,
        comment="The end user who made the request"
    )

    # ==================== Context: WHERE ====================
    # The Target Resource Group - where limits are enforced (from X-RG-ID header)
    group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment="The target group where limits were enforced (from X-RG-ID)"
    )

    # HIERARCHICAL ATTRIBUTION
    # Stores the full ancestry path for instant roll-up queries
    # Example: "org_root/dept_engineering/team_frontend"
    ancestry_path = Column(
        String(1000),
        nullable=True,
        comment="Full hierarchy path (e.g., 'org/dept/team') for roll-up reporting"
    )

    # Resource Group metadata snapshot (for historical accuracy if group is deleted)
    group_name = Column(
        String(255),
        nullable=True,
        comment="Group name at time of request (snapshot)"
    )

    # ==================== Resource: WHAT ====================
    resource_type = Column(
        String(50),
        nullable=False,
        comment="Type of resource: 'llm', 'mcp', 'api_key'"
    )

    resource_id = Column(
        String(255),
        nullable=False,
        comment="ID of the specific LLM model or MCP server config"
    )

    # Resource metadata snapshot
    resource_name = Column(
        String(255),
        nullable=True,
        comment="Resource name at time of request (snapshot), LLM model name, MCP Server name"
    )

    # Was this resource inherited from a parent group?
    is_inherited = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="True if resource was inherited from parent group"
    )

    # If inherited, which group did it come from?
    inherited_from_group_id = Column(
        String(255),
        nullable=True,
        comment="Group ID that provided the resource (if inherited)"
    )

    # Agent Skills Attribution
    skill_id = Column(
        String(255),
        nullable=True,
        comment="Agent Skill ID when resource_type='skill'"
    )

    skill_execution_id = Column(
        String(255),
        nullable=True,
        comment="Skill execution ID for cross-table correlation"
    )

    # ==================== LLM Usage Metrics ====================
    # Token counts (null for MCP requests)
    prompt_tokens = Column(
        Integer,
        default=0,
        comment="Input tokens consumed (LLM only)"
    )

    completion_tokens = Column(
        Integer,
        default=0,
        comment="Output tokens generated (LLM only)"
    )

    total_tokens = Column(
        Integer,
        default=0,
        comment="Total tokens = prompt + completion (LLM only)"
    )

    # Model details
    model_name = Column(
        String(255),
        comment="Provider model string (e.g., 'gpt-4o', 'claude-sonnet-4')"
    )

    provider = Column(
        String(50),
        comment="Provider name: 'openai', 'anthropic', 'google', etc."
    )

    # ==================== MCP Usage Metrics ====================

    mcp_tool_name = Column(
        String(255),
        nullable=True,
        comment="MCP tool name invoked (e.g., 'read_file', 'sql_query')"
    )

    mcp_execution_time_ms = Column(
        Integer,
        nullable=True,
        comment="MCP tool execution time in milliseconds"
    )

    mcp_call_count = Column(
        Integer,
        default=1,
        comment="Number of MCP tool calls in this request"
    )

    mcp_data_size_bytes = Column(
        Integer,
        nullable=True,
        comment="Size of data returned by MCP tool in bytes"
    )

    # ==================== Cost Tracking ====================
    estimated_cost = Column(
        Float,
        default=0.0,
        comment="Estimated cost in specified currency"
    )

    currency = Column(
        String(10),
        default="USD",
        comment="Currency code (USD, EUR, etc.)"
    )

    # Cost breakdown for transparency
    prompt_cost = Column(
        Float,
        default=0.0,
        comment="Cost of input tokens (LLM only)"
    )

    completion_cost = Column(
        Float,
        default=0.0,
        comment="Cost of output tokens (LLM only)"
    )

    # ==================== Request/Response Details ====================
    request_id = Column(
        String(255),
        unique=True,
        nullable=False,
        comment="Gateway request ID for tracing (UUID)"
    )

    status_code = Column(
        Integer,
        comment="HTTP status code of the response"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if request failed"
    )

    response_time_ms = Column(
        Integer,
        comment="Total response time in milliseconds"
    )

    # HTTP method and endpoint
    http_method = Column(
        String(10),
        comment="HTTP method: GET, POST, etc."
    )

    endpoint = Column(
        String(255),
        comment="API endpoint called"
    )

    # ==================== Client Information ====================
    client_ip = Column(
        String(45),  # IPv6 support
        nullable=True,
        comment="Client IP address"
    )

    user_agent = Column(
        String(255),
        nullable=True,
        comment="Client user agent string"
    )

    # ==================== Provenance (Traffic Data Import) ====================
    # 'gateway' = governed inline traffic (the default; written by the Rust
    # proxy). 'import:<api>' = observed telemetry pulled from a vendor admin/
    # analytics API. This is the load-bearing observability-vs-governance seam:
    # imported rows must never drive inline enforcement.
    source = Column(
        String(40),
        server_default="gateway",
        default="gateway",
        nullable=False,
        comment="gateway | import:<import_api>",
    )
    ingest_grain = Column(
        String(16),
        nullable=True,
        comment="ISO-8601 duration of the imported aggregate bucket (NULL for inline)",
    )
    import_run_id = Column(
        String(64),
        nullable=True,
        comment="usage_import_run_logs.id that produced this row (NULL for inline)",
    )

    # ==================== Timestamps ====================
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the request was made"
    )

    # ==================== Relationships ====================
    user = relationship("EndUser", backref="usage_logs")
    group = relationship("ResourceGroup", backref="usage_logs")

    # ==================== Indexes and Constraints ====================
    __table_args__ = (
        # PRIMARY QUOTA QUERY: "What's today's usage for this group?"
        # This is THE critical index for quota enforcement
        Index('ix_usage_quota_check',
              'tenant_id', 'group_id', 'resource_type', 'created_at'),

        # HIERARCHICAL ROLL-UP: "What's the total spend for Department?"
        # Enables: WHERE ancestry_path LIKE 'org/dept/%'
        Index('ix_usage_ancestry_rollup',
              'tenant_id', 'ancestry_path', 'created_at'),

        # USER REPORTING: "Show me my usage history"
        Index('ix_usage_user_time',
              'tenant_id', 'user_id', 'created_at'),

        # RESOURCE ANALYSIS: "How much are we spending on GPT-4?"
        Index('ix_usage_resource_analysis',
              'tenant_id', 'resource_type', 'resource_id', 'created_at'),

        # COST TRACKING: "Show me the most expensive requests"
        Index('ix_usage_cost_analysis',
              'tenant_id', 'estimated_cost', 'created_at'),

        # ERROR MONITORING: "Show me failed requests"
        Index('ix_usage_error_tracking',
              'tenant_id', 'status_code', 'created_at'),

        # PROVIDER COMPARISON: "OpenAI vs Anthropic usage"
        Index('ix_usage_provider',
              'tenant_id', 'provider', 'created_at'),

        # MCP ANALYTICS: "Which tools are used most?"
        Index('ix_usage_mcp_tools',
              'tenant_id', 'mcp_tool_name', 'created_at'),

        # REQUEST TRACING: "Find request by ID"
        Index('ix_usage_request', 'request_id'),

        # INHERITANCE ANALYSIS: "How much usage is from inherited resources?"
        Index('ix_usage_inheritance',
              'tenant_id', 'is_inherited', 'created_at'),

        # SKILL QUOTA ENFORCEMENT: "Get skill usage for group today"
        Index('ix_usage_skill_quota',
              'tenant_id', 'group_id', 'skill_id', 'created_at'),

        # SKILL EXECUTION TRACING: "Find usage log by skill execution"
        Index('ix_usage_skill_execution', 'skill_execution_id'),

        # TRAFFIC IMPORT / SOURCE FILTER: "vendor-imported vs gateway-native
        # usage over time". Created by the migration too; declared here so a
        # fresh create_all install (which builds from the ORM, not the
        # migrations) gets it too — otherwise source-filtered queries do a
        # sequential scan on fresh deployments.
        Index('ix_usage_logs_source_created',
              'tenant_id', 'source', 'created_at'),

        {
            'comment': 'Transaction ledger for AI Gateway - tracks quotas, costs, and usage'
        }
    )

    # ==================== Methods ====================

    def __repr__(self) -> str:
        return (
            f"<UsageLog(id={self.id}, user_id={self.user_id}, "
            f"group_id='{self.group_id}', resource='{self.resource_type}:{self.resource_id}')>"
        )

    @property
    def is_llm_request(self) -> bool:
        """Check if this is an LLM request"""
        return self.resource_type == 'llm'

    @property
    def is_mcp_request(self) -> bool:
        """Check if this is an MCP request"""
        return self.resource_type == 'mcp'

    @property
    def is_skill_request(self) -> bool:
        """Check if this is an Agent Skill request"""
        return self.resource_type == 'skill'

    @property
    def is_success(self) -> bool:
        """Check if request was successful"""
        return self.status_code and 200 <= self.status_code < 300

    @property
    def is_error(self) -> bool:
        """Check if request resulted in error"""
        return self.status_code and self.status_code >= 400

    @property
    def group_hierarchy_levels(self) -> List[str]:
        """
        Get all hierarchy levels from ancestry path

        Returns:
            List of group IDs from root to target
            Example: ['org_root', 'dept_eng', 'team_frontend']
        """
        if not self.ancestry_path:
            return [self.group_id] if self.group_id else []
        return self.ancestry_path.split('/')

    def to_dict(self, include_details: bool = False) -> Dict[str, Any]:
        """
        Convert usage log to dictionary

        Args:
            include_details: Include detailed metadata (error messages, etc.)
        """
        data = {
            "id": self.id,
            "timestamp": self.created_at.isoformat() if self.created_at else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "resource": {
                "type": self.resource_type,
                "id": self.resource_id,
                "name": self.resource_name,
                "is_inherited": self.is_inherited
            },
            "status_code": self.status_code,
            "success": self.is_success
        }

        # Add LLM-specific data
        if self.is_llm_request:
            data["llm"] = {
                "model": self.model_name,
                "provider": self.provider,
                "tokens": {
                    "prompt": self.prompt_tokens,
                    "completion": self.completion_tokens,
                    "total": self.total_tokens
                },
                "cost": {
                    "prompt": self.prompt_cost,
                    "completion": self.completion_cost,
                    "total": self.estimated_cost,
                    "currency": self.currency
                }
            }

        # Add MCP-specific data
        if self.is_mcp_request:
            data["mcp"] = {
                "tool": self.mcp_tool_name,
                "execution_time_ms": self.mcp_execution_time_ms,
                "call_count": self.mcp_call_count,
                "data_size_bytes": self.mcp_data_size_bytes
            }

        # Add skill-specific data
        if self.is_skill_request:
            data["skill"] = {
                "skill_id": self.skill_id,
                "skill_execution_id": self.skill_execution_id,
            }

        if include_details:
            data.update({
                "request_id": self.request_id,
                "source": self.source,
                "ingest_grain": self.ingest_grain,
                "import_run_id": self.import_run_id,
                "ancestry_path": self.ancestry_path,
                "response_time_ms": self.response_time_ms,
                "error_message": self.error_message,
                "http_method": self.http_method,
                "endpoint": self.endpoint,
                "client_ip": self.client_ip,
                "user_agent": self.user_agent,
                "inherited_from": self.inherited_from_group_id
            })

        return data
