# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Universal Proxy Metrics Database Model
SQLAlchemy model for detailed proxy performance metrics across ALL proxy types

Supports MCP, LLM, OAuth, and Virtual MCP proxies
"""

from typing import Dict, Any

from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, Text, Index, ForeignKey, JSON

from app.database import Base


class ProxyMetricEntry(Base):
    """
    SQLAlchemy model for detailed proxy metrics - UNIVERSAL for all proxy types

    Supports:
    - MCP Proxy
    - LLM Proxy
    - OAuth Proxy
    - Virtual MCP Proxy
    """
    __tablename__ = 'proxy_metrics'

    id = Column(String(255), primary_key=True)

    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    timestamp = Column(DateTime(timezone=True), nullable=False)

    # ======================================================================
    # PROXY TYPE CLASSIFICATION (NEW - Universal Support)
    # ======================================================================

    proxy_type = Column(
        String(20),
        nullable=False,
        comment="Proxy type: mcp, llm, oauth, virtual_mcp"
    )

    # Provenance plane: 'gateway' = governed inline traffic (written by the
    # Rust proxy, relies on the server_default), 'observed' = imported (Traffic
    # Data Import) telemetry mirrored from a vendor admin API. Drives Mission
    # Control's Governed / Observed / All filter.
    source = Column(
        String(40),
        nullable=False,
        server_default="gateway",
        comment="Provenance plane: 'gateway' (governed inline) or 'observed' (imported)",
    )

    # Request identification
    request_id = Column(String(255), nullable=False)
    correlation_id = Column(String(255), nullable=True)

    # ======================================================================
    # END-USER CONTEXT (Common to all proxies)
    # ======================================================================

    end_user_type = Column(String(20), nullable=False)  # anonymous, api_key, oauth
    end_user_username = Column(String(255), nullable=True)
    gateway_user_id = Column(String(255), nullable=True)
    gateway_username = Column(String(255), nullable=True)
    client_ip = Column(String(45), nullable=True)
    user_agent_hash = Column(String(64), nullable=True)  # SHA256 hash for privacy

    # ======================================================================
    # SERVER AND ROUTING (Common to all proxies)
    # ======================================================================

    server_id = Column(String(255), nullable=True)
    virtual_server_id = Column(String(255), nullable=True)
    server_url = Column(Text, nullable=True)

    # ======================================================================
    # REQUEST DETAILS (Common to all proxies)
    # ======================================================================

    http_method = Column(String(10), nullable=False)
    endpoint = Column(Text, nullable=True)
    endpoint_category = Column(String(50), nullable=True)  # mcp_proxy, llm_proxy, oauth_proxy, etc.

    # ======================================================================
    # PERFORMANCE METRICS (Common to all proxies)
    # ======================================================================

    total_duration_ms = Column(Float, nullable=False)
    connect_time_ms = Column(Float, nullable=True)
    handshake_time_ms = Column(Float, nullable=True)
    processing_time_ms = Column(Float, nullable=True)
    response_time_ms = Column(Float, nullable=True)

    # ======================================================================
    # SIZE METRICS (Common to all proxies)
    # ======================================================================

    request_size_bytes = Column(Integer, nullable=True)
    response_size_bytes = Column(Integer, nullable=True)
    request_headers_count = Column(Integer, nullable=True)
    response_headers_count = Column(Integer, nullable=True)

    # ======================================================================
    # STATUS AND ERRORS (Common to all proxies)
    # ======================================================================

    http_status_code = Column(Integer, nullable=True)
    success = Column(Boolean, nullable=False)
    error_type = Column(String(100), nullable=True)
    error_category = Column(String(50), nullable=True)  # network, auth, protocol, server
    retry_count = Column(Integer, nullable=True)

    # ======================================================================
    # TYPE-SPECIFIC DATA (JSON Fields)
    # ======================================================================

    mcp_data = Column(
        JSON,
        nullable=True,
        comment="MCP-specific data: method, tool_name, resource_uri, prompt_name, protocol_version, session_reused"
    )

    llm_data = Column(
        JSON,
        nullable=True,
        comment="LLM-specific data: provider, model_requested, model_used, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd, temperature, max_tokens, streaming, function_calling"
    )

    oauth_data = Column(
        JSON,
        nullable=True,
        comment="OAuth-specific data: idp_id, flow_type, grant_type, scope, token_type"
    )

    virtual_mcp_data = Column(
        JSON,
        nullable=True,
        comment="Virtual MCP-specific data: source_servers, aggregated_capabilities"
    )

    skill_data = Column(
        JSON,
        nullable=True,
        comment="""Skill-specific metrics data (same pattern as mcp_data, llm_data, etc.):
        {
            "skill_id": "skill-01J...",
            "skill_name": "query-sales-data",
            "skill_version": "1.0.0",
            "execution_id": "ase-01J...",
            "step_name": "query_data",
            "is_dependency": false
        }"""
    )

    # ======================================================================
    # NETWORK AND INFRASTRUCTURE (Common to all proxies)
    # ======================================================================

    dns_resolution_time_ms = Column(Float, nullable=True)
    ssl_handshake_time_ms = Column(Float, nullable=True)
    tcp_connect_time_ms = Column(Float, nullable=True)
    server_response_headers = Column(Text, nullable=True)  # JSON

    # ======================================================================
    # BUSINESS METRICS (Common to all proxies)
    # ======================================================================

    cache_hit = Column(Boolean, nullable=True)
    rate_limited = Column(Boolean, nullable=True)
    authentication_time_ms = Column(Float, nullable=True)

    # ======================================================================
    # RESOURCE UTILIZATION (Common to all proxies)
    # ======================================================================

    memory_usage_mb = Column(Float, nullable=True)
    cpu_usage_percent = Column(Float, nullable=True)

    # ======================================================================
    # GEOGRAPHIC AND TIMING (Common to all proxies)
    # ======================================================================

    request_hour = Column(Integer, nullable=False)  # 0-23 for pattern analysis
    request_day_of_week = Column(Integer, nullable=False)  # 0-6
    timezone_offset = Column(Integer, nullable=True)

    # ======================================================================
    # INDEXES (Optimized for universal queries)
    # ======================================================================

    __table_args__ = (
        # ===== UNIVERSAL QUERIES =====

        # Time-series queries with tenant isolation
        Index('ix_proxy_metrics_tenant_timestamp', 'tenant_id', 'timestamp'),

        # By proxy type - "Show LLM metrics for last 24h"
        Index('ix_proxy_metrics_tenant_type_timestamp', 'tenant_id', 'proxy_type', 'timestamp'),

        # Success rate analysis by type - "Show LLM errors in last hour"
        Index('ix_proxy_metrics_tenant_type_success_timestamp', 'tenant_id', 'proxy_type', 'success', 'timestamp'),

        # ===== COMMON QUERIES =====

        # Server performance metrics - "Show metrics for specific server"
        Index('ix_proxy_metrics_tenant_server_timestamp', 'tenant_id', 'server_id', 'timestamp'),
        Index('ix_proxy_metrics_tenant_vserver_timestamp', 'tenant_id', 'virtual_server_id', 'timestamp'),

        # User activity analysis - "Show user's request history"
        Index('ix_proxy_metrics_tenant_user_timestamp', 'tenant_id', 'end_user_username', 'timestamp'),
        Index('ix_proxy_metrics_tenant_gateway_user_timestamp', 'tenant_id', 'gateway_username', 'timestamp'),

        # User type analysis - "Compare anonymous vs authenticated requests"
        Index('ix_proxy_metrics_tenant_usertype_timestamp', 'tenant_id', 'end_user_type', 'timestamp'),

        # Error analysis - "Show all errors by type"
        Index('ix_proxy_metrics_tenant_error_timestamp', 'tenant_id', 'error_category', 'timestamp'),

        # HTTP status analysis - "Show 5xx errors"
        Index('ix_proxy_metrics_tenant_status_timestamp', 'tenant_id', 'http_status_code', 'timestamp'),

        # Performance percentile queries - "Find slow requests"
        Index('ix_proxy_metrics_tenant_duration', 'tenant_id', 'total_duration_ms', 'timestamp'),

        # Request tracking - "Find request by ID"
        Index('ix_proxy_metrics_tenant_request_id', 'tenant_id', 'request_id'),

        # Correlation tracking - "Find related requests"
        Index('ix_proxy_metrics_tenant_correlation_id', 'tenant_id', 'correlation_id'),

        # Time pattern analysis - "Peak hours analysis"
        Index('ix_proxy_metrics_tenant_hour', 'tenant_id', 'request_hour', 'timestamp'),
        Index('ix_proxy_metrics_tenant_dow', 'tenant_id', 'request_day_of_week', 'timestamp'),

        # Endpoint category analysis
        Index('ix_proxy_metrics_tenant_endpoint_category', 'tenant_id', 'endpoint_category', 'timestamp'),

        # Governed / Observed plane filter (Mission Control source toggle)
        Index('ix_proxy_metrics_tenant_source_timestamp', 'tenant_id', 'source', 'timestamp'),

        # AGENT SKILLS EXTENSION
        # Note: skill_data is JSON and not directly indexable. Queries on skill_data
        # fields should use JSON path expressions. For PostgreSQL, a GIN index on
        # skill_data can be added via raw DDL if needed for high-volume deployments.

        {
            'comment': 'Universal performance metrics for all proxy types (MCP, LLM, OAuth, Virtual MCP)'
        }
    )

    def __repr__(self):
        return f"<ProxyMetricEntry(id={self.id}, type={self.proxy_type}, timestamp={self.timestamp}, success={self.success})>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        result = {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "tenant_id": self.tenant_id,
            "proxy_type": self.proxy_type,
            "source": self.source,

            # Request identification
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,

            # User context
            "end_user_type": self.end_user_type,
            "end_user_username": self.end_user_username,
            "gateway_user_id": self.gateway_user_id,
            "gateway_username": self.gateway_username,
            "client_ip": self.client_ip,

            # Server
            "server_id": self.server_id,
            "virtual_server_id": self.virtual_server_id,

            # Request
            "http_method": self.http_method,
            "endpoint_category": self.endpoint_category,

            # Performance
            "total_duration_ms": self.total_duration_ms,

            # Status
            "http_status_code": self.http_status_code,
            "success": self.success,
            "error_type": self.error_type,
            "error_category": self.error_category,
        }

        # Add type-specific data if present
        if self.mcp_data:
            result["mcp"] = self.mcp_data
        if self.llm_data:
            result["llm"] = self.llm_data
        if self.oauth_data:
            result["oauth"] = self.oauth_data
        if self.virtual_mcp_data:
            result["virtual_mcp"] = self.virtual_mcp_data
        if self.skill_data:
            result["skill"] = self.skill_data

        return result