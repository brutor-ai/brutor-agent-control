# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Database Model
"""

from sqlalchemy import CheckConstraint, Column, String, Boolean, Integer, Float, JSON, DateTime, LargeBinary, Text, ForeignKey, Index
from sqlalchemy.sql import func
from sqlalchemy.orm import validates
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from app.constants.endpoints import EndpointUrls
from app.database.models.resource_mixins import ResourceAuthorshipMixin, ResourceVersionMixin
from app.database import Base

#: `mcp_servers.max_concurrent_requests`, as the column says it.
MAX_CONCURRENT_REQUESTS_COMMENT = (
    "Requests to this server in flight at once on one gateway instance; "
    "NULL is 10. A positive whole number."
)


class MCPServer(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """MCP Server configuration model"""

    __tablename__ = "mcp_servers"

    # Basic Information
    id = Column(String(255), primary_key=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    # Connection Details
    base_url = Column(String(500), nullable=False)
    mcp_endpoint_path = Column(String(200), default="", nullable=False)

    # Authentication
    auth_type = Column(String(50), default="none")
    # Ciphertext of the encryption service (`app/services/mcp/server/server_token.py`).
    auth_token = Column(Text, nullable=True)

    # Configuration
    timeout = Column(Float, default=30.0)
    # Read by the gateway as a semaphore of its process (`mcp_limits::
    # DEFAULT_MAX_CONCURRENT_REQUESTS` when NULL); a positive whole number.
    max_concurrent_requests = Column(Integer, nullable=True, comment=MAX_CONCURRENT_REQUESTS_COMMENT)

    # Resilience config (per-server overrides for circuit breaker + retry)
    retry_attempts = Column(Integer, default=2, comment="Max retry attempts for transient failures")
    retry_after_ms = Column(Integer, default=100, comment="Base delay in milliseconds between retries")
    failure_threshold = Column(Integer, default=5, comment="Consecutive failures before circuit breaker opens")
    recovery_timeout_secs = Column(Integer, default=30, comment="Seconds before circuit breaker transitions to half-open")

    enabled = Column(Boolean, default=True)
    # MCP Apps governance — when False, the gateway refuses to serve
    # the bundled `_meta.ui.resourceUri` for this server, and strips
    # the `_meta.ui.*` block from tool-call responses so the portal
    # doesn't even attempt to mount an iframe. Default True (apps
    # enabled). Operators flip this to False for compliance-bound
    # tenants who don't want iframe-rendered tool output.
    mcp_apps_enabled = Column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
        comment="When False, gateway suppresses MCP-Apps UI bundles for this server",
    )

    # Tenant-wide default limit/policy posture for this MCP server (min-wins
    # with each Resource Group). Same shapes the RG MCP forms use
    # (mcp_global_limits incl. per_tool / mcp_global_governance). The Rust proxy
    # folds these into the effective (RG, server) limits/governance with
    # min-wins. Set via Resources → MCP Servers → server edit → Limits/Policies.
    tenant_limits = Column(JSON(none_as_null=True), nullable=True)
    tenant_governance = Column(JSON(none_as_null=True), nullable=True)

    # Capabilities
    capabilities = Column(JSON, nullable=True)
    discovered_at = Column(DateTime(timezone=True), nullable=True)
    server_info = Column(JSON, nullable=True)

    # MCP wire dialect (handshake, or the stateless MCP revision >= 2026-07-28).
    # Written by the Rust gateway at capability discovery/refresh; the
    # proxy speaks whichever dialect each upstream supports. Deliberately
    # NOT surfaced in to_dict()/response models.
    protocol_version = Column(
        String(16),
        nullable=True,
        comment=(
            "Newest MCP protocol version this upstream negotiated "
            "(e.g. 2025-03-26, 2026-07-28). NULL = not yet discovered."
        ),
    )
    dialect = Column(
        String(16),
        nullable=True,
        comment=(
            "MCP wire dialect the upstream speaks: 'handshake' "
            "(initialize/session, <= 2025-11-25) or 'stateless' "
            "(>= 2026-07-28). NULL = not yet discovered."
        ),
    )

    # Streamable-HTTP listener affinity. Set to False when the
    # upstream's `initialize` response declares no
    # listChanged/subscribe/logging capabilities — i.e. it has
    # nothing to push to clients and the optional SSE listener
    # channel is provably useless against it. The Rust gateway
    # short-circuits listener GETs to 405 when this is False, and
    # the portal SDK skips opening the listener entirely, which
    # together prevent the SSE reconnect loop that otherwise
    # pumps hundreds of junk req/h per non-listener server.
    # Refreshed on each capability discovery; defaults True on
    # legacy rows so behaviour is unchanged until the next probe.
    wants_listener = Column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
        comment=(
            "Does this server want its SSE listener channel opened? "
            "Derived from initialize-response capabilities; gates "
            "the gateway's 405 short-circuit and the portal SDK's "
            "listener-stream policy."
        ),
    )

    # Health Status
    health_status = Column(String(20), default="unknown")
    last_health_check = Column(DateTime(timezone=True), nullable=True)
    last_health_success = Column(DateTime(timezone=True), nullable=True)
    consecutive_health_failures = Column(Integer, default=0)
    health_check_error = Column(Text, nullable=True)

    # Icon Storage - Database based
    icon_data = Column(LargeBinary, nullable=True)  # Stores actual binary data
    icon_mime_type = Column(String(100), nullable=True)  # e.g., 'image/png'
    icon_size = Column(Integer, nullable=True)  # Size in bytes
    icon_hash = Column(String(64), nullable=True)  # SHA256 hash for deduplication
    icon_uploaded_at = Column(DateTime(timezone=True), nullable=True)

    # OAuth Configuration
    idp_id = Column(String(255), nullable=True)  # Reference to IdPServer

    # Data residency
    # Region this MCP server serves from. NULL = unspecified; the
    # tenant's data_residency profile decides whether unspecified
    # is allowed. Same shape as llm_providers.region.
    region = Column(
        String(32),
        nullable=True,
        comment=(
            "Geographic region this MCP server serves from. Read by "
            "the Rust residency check; NULL = unspecified."
        ),
    )

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    server_configs = relationship(
        "ServerConfig",
        back_populates="mcp_server",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    docker_deployment = relationship(
        "DockerDeployment",
        back_populates="mcp_server",
        uselist=False,
        cascade="all, delete-orphan"
    )

    kubernetes_deployment = relationship(
        "KubernetesDeployment",
        back_populates="mcp_server",
        uselist=False,
        cascade="all, delete-orphan"
    )

    ecs_deployment = relationship(
        "EcsDeployment",
        back_populates="mcp_server",
        uselist=False,
        cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        # Multi-tenant queries - most common access pattern
        Index('ix_mcp_servers_tenant_enabled', 'tenant_id', 'enabled'),

        # Server lookup by name within tenant
        Index('ix_mcp_servers_tenant_name', 'tenant_id', 'name'),

        # Health monitoring queries
        Index('ix_mcp_servers_health_status', 'tenant_id', 'health_status', 'enabled'),
        Index('ix_mcp_servers_health_check', 'tenant_id', 'last_health_check'),
        Index('ix_mcp_servers_health_failures', 'consecutive_health_failures', 'enabled'),

        CheckConstraint(
            'max_concurrent_requests IS NULL OR max_concurrent_requests > 0',
            name='ck_mcp_servers_max_concurrent_requests_positive',
        ),

        # Icon deduplication lookups
        Index('ix_mcp_servers_icon_hash', 'icon_hash'),

        # OAuth/IdP integration queries
        Index('ix_mcp_servers_idp_id', 'idp_id'),

        # Capability discovery queries
        Index('ix_mcp_servers_discovered_at', 'tenant_id', 'discovered_at'),

        # Audit and history queries
        Index('ix_mcp_servers_created_at', 'tenant_id', 'created_at'),
    )

    @validates('capabilities')
    def validate_capabilities(self, key, value):
        """Every write of the capabilities column is normalised.

        The runtime reads tool `annotations` from this column (autonomy
        gating, `deny_tool_risk`, evidence attribution) and treats an
        unannotated tool as side-effecting, so a writer that strips fields
        silently turns read-only tools into writes. Normalising here means
        no ORM writer can skip it; the bypass paths (raw SQL, Core
        `update().values()`, bulk mappings) are banned by
        tests/test_mcp_capability_writers_guard.py.
        """
        from app.services.mcp.tool_snapshot import normalize_mcp_capabilities
        return normalize_mcp_capabilities(value)

    @validates('base_url')
    def validate_base_url(self, key, value):
        """Ensure base_url doesn't have trailing slash"""
        if value and value.endswith('/'):
            return value.rstrip('/')
        return value

    @property
    def mcp_url(self) -> str:
        """Get full MCP endpoint URL"""
        if self.mcp_endpoint_path:
            # Ensure single slash between base_url and path
            path = self.mcp_endpoint_path.lstrip('/')
            return f"{self.base_url}/{path}"
        return self.base_url

    @property
    def server_base(self) -> str:
        """Get base server URL without MCP endpoint path"""
        return self.base_url

    # =========================================================================
    # Capability Methods
    # =========================================================================

    def has_capabilities(self) -> bool:
        """Check if capabilities have been discovered"""
        # Check for None OR empty dict
        has_caps = (
                self.capabilities is not None and
                self.capabilities != {} # handle empty dict
        )
        return has_caps

    def update_capabilities(
        self,
        capabilities: Dict[str, Any],
        server_info: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Update server capabilities and metadata

        Args:
            capabilities: Discovered capabilities (tools, resources, prompts, etc.)
            server_info: Server information from initialization

        The assignment runs through `validate_capabilities`, which applies
        the canonical tool normaliser (app/services/mcp/tool_snapshot.py).
        """
        self.capabilities = capabilities
        self.discovered_at = datetime.now(timezone.utc)

        if server_info:
            self.server_info = server_info

        self.updated_at = datetime.now(timezone.utc)

    # =========================================================================
    # Health Status Methods
    # =========================================================================

    def update_health_status(
        self,
        status: str,
        response_time_ms: Optional[float] = None,
        error_message: Optional[str] = None
    ) -> None:
        """
        Update server health status

        Args:
            status: Health status ('healthy', 'unhealthy', 'unknown')
            response_time_ms: Response time in milliseconds
            error_message: Error message if unhealthy
        """
        self.health_status = status
        self.last_health_check = datetime.now(timezone.utc)

        if status == "healthy":
            self.last_health_success = datetime.now(timezone.utc)
            self.consecutive_health_failures = 0
            self.health_check_error = None
        else:
            self.consecutive_health_failures += 1
            self.health_check_error = error_message

        self.updated_at = datetime.now(timezone.utc)

    def get_health_status_dict(self) -> Dict[str, Any]:
        """
        Get health status as dictionary for API responses

        Returns:
            Dictionary with health status information
        """
        return {
            "status": self.health_status or "unknown",
            "last_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "last_success": self.last_health_success.isoformat() if self.last_health_success else None,
            "consecutive_failures": self.consecutive_health_failures or 0,
            "error_message": self.health_check_error
        }

    # =========================================================================
    # Icon Methods
    # =========================================================================

    def has_icon(self) -> bool:
        """Check if server has an icon"""
        return self.icon_data is not None

    def get_icon_info(self) -> Optional[Dict[str, Any]]:
        """Get icon metadata"""
        if not self.has_icon():
            return None

        return {
            "type": "file",
            "mime_type": self.icon_mime_type,
            "size": self.icon_size,
            "hash": self.icon_hash,
            "uploaded_at": self.icon_uploaded_at.isoformat() if self.icon_uploaded_at else None,
            "url": f"{EndpointUrls.MCP_SERVER_ADMIN.value}/{self.id}/icon"
        }

    # =========================================================================
    # Serialization
    # =========================================================================

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            # AI Resource identity block.
            "version": self.version,
            **self.authorship_dict(),
            "base_url": self.base_url,
            "mcp_endpoint_path": self.mcp_endpoint_path,
            "url": self.mcp_url,
            "auth_type": self.auth_type,
            "timeout": self.timeout,
            "max_concurrent_requests": self.max_concurrent_requests,
            "enabled": self.enabled,
            "mcp_apps_enabled": bool(self.mcp_apps_enabled),
            "tenant_limits": self.tenant_limits,
            "tenant_governance": self.tenant_governance,
            "capabilities": self.capabilities,
            "discovered_at": self.discovered_at.isoformat() if self.discovered_at else None,
            # Surface the listener-affinity flag in the API
            # response so the portal can configure its SDK
            # transport accordingly. Defaults to True for
            # legacy rows that haven't been re-probed yet —
            # safe because the worst case is that the SDK
            # opens a listener that gets 405'd by the
            # gateway gate.
            "wants_listener": bool(self.wants_listener) if self.wants_listener is not None else True,
            "protocol_version": self.protocol_version,
            "dialect": self.dialect,
            "server_info": self.server_info,
            "health_status": self.get_health_status_dict(),
            "icon": self.get_icon_info(),
            "region": self.region,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

        # The static token is never part of any representation: whether one
        # is stored is all that is said.
        data["has_auth_token"] = bool(self.auth_token)

        return data


# The static token is sent as a bearer: a server that authenticates another
# way has none. Held on the row itself, so that every writer is held to it
# (the API, a deployment, a catalog import, a seed): a token stored while
# the server was a bearer server does not survive a change away from it.
BEARER_AUTH_TYPE = "bearer"


def _no_token_unless_bearer(_mapper, _connection, server) -> None:
    if server.auth_type != BEARER_AUTH_TYPE and server.auth_token is not None:
        server.auth_token = None


from sqlalchemy import event as _event  # noqa: E402

_event.listen(MCPServer, "before_insert", _no_token_unless_bearer)
_event.listen(MCPServer, "before_update", _no_token_unless_bearer)
