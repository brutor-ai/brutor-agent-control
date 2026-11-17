# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Docker Deployment Database Models
Track Docker images, containers, and deployment history
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional

from sqlalchemy import Column, String, Text, DateTime, JSON, Integer, Float, Boolean, Index, UniqueConstraint, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class DockerImage(Base):
    """
    Docker images built or pulled by the gateway

    NOTE: This table is OPTIONAL and primarily used for tracking build history.
    Deployments can exist without entries in this table (e.g., pre-existing images).
    """
    __tablename__ = "docker_images"

    # Primary Key
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    # Image identification
    image_id = Column(
        String(64),
        nullable=False,
        comment="Docker image ID (sha256 hash)"
    )

    repository = Column(
        String(500),
        nullable=False,
        comment="Image repository (e.g., 'google-workspace-mcp')"
    )

    tag = Column(
        String(100),
        nullable=False,
        default="latest",
        comment="Image tag"
    )

    # Full image name for reference
    full_name = Column(
        String(600),
        nullable=False,
        comment="Full image name with tag (e.g., 'google-workspace-mcp:latest')"
    )

    # Image metadata
    size_bytes = Column(
        Integer,
        nullable=False,
        comment="Image size in bytes"
    )

    created_at = Column(
        DateTime,
        nullable=False,
        comment="When the image was created (from Docker)"
    )

    # Build information
    build_method = Column(
        String(50),
        nullable=False,
        comment="How image was obtained: 'built', 'pulled', 'github'"
    )

    dockerfile_content = Column(
        Text,
        nullable=True,
        comment="Dockerfile content if built locally"
    )

    build_args = Column(
        JSON,
        nullable=True,
        comment="Build arguments used"
    )

    build_logs = Column(
        Text,
        nullable=True,
        comment="Build logs (truncated)"
    )

    # GitHub build information
    github_repo = Column(
        String(500),
        nullable=True,
        comment="GitHub repository URL if built from GitHub"
    )

    github_branch = Column(
        String(100),
        nullable=True,
        comment="GitHub branch used for build"
    )

    build_path = Column(
        String(500),
        nullable=True,
        comment="Local path where GitHub repo was cloned and built"
    )

    # Management
    managed_by = Column(
        String(50),
        nullable=True,
        comment="Admin user who built/pulled this image"
    )

    imported_at = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="When image was imported to gateway"
    )

    last_used = Column(
        DateTime,
        nullable=True,
        comment="Last time image was used to create a container"
    )

    # Status
    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether image is still available"
    )

    # Relationships
    deployments = relationship(
        "DockerDeployment",
        back_populates="image",
        lazy="dynamic"
    )

    kubernetes_deployments = relationship(
        "KubernetesDeployment",
        back_populates="image",
        lazy="dynamic"
    )

    __table_args__ = (
        # image_id should be unique within tenant, not globally
        UniqueConstraint('tenant_id', 'image_id', name='uix_docker_images_tenant_image_id'),

        # Primary queries - "Get all active images for tenant"
        Index('ix_docker_images_tenant_active', 'tenant_id', 'is_active'),

        # Image lookup by repository within tenant
        Index('ix_docker_images_tenant_repo', 'tenant_id', 'repository'),
        Index('ix_docker_images_tenant_repo_tag', 'tenant_id', 'repository', 'tag'),

        # Build method filtering - "Show all pulled images"
        Index('ix_docker_images_tenant_build_method', 'tenant_id', 'build_method'),

        # GitHub source tracking - "Find images from specific repo"
        Index('ix_docker_images_tenant_github', 'tenant_id', 'github_repo'),

        # Usage tracking - "Find unused images"
        Index('ix_docker_images_tenant_last_used', 'tenant_id', 'last_used'),

        # Import history
        Index('ix_docker_images_tenant_imported', 'tenant_id', 'imported_at'),

        {
            'comment': 'Docker images managed by the gateway (optional tracking)'
        }
    )

    def __repr__(self) -> str:
        return f"<DockerImage(id={self.id}, name='{self.full_name}', size_mb={self.size_mb})>"

    @property
    def size_mb(self) -> float:
        """Get size in megabytes"""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "image_id": self.image_id,
            "repository": self.repository,
            "tag": self.tag,
            "full_name": self.full_name,
            "size_bytes": self.size_bytes,
            "size_mb": self.size_mb,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "build_method": self.build_method,
            "github_repo": self.github_repo,
            "github_branch": self.github_branch,
            "build_path": self.build_path,
            "managed_by": self.managed_by,
            "imported_at": self.imported_at.isoformat() if self.imported_at else None,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "is_active": self.is_active,
            "deployment_count": self.deployments.count()
        }


class DockerDeployment(Base):
    """
    Docker container deployments for MCP servers

    Tracks the relationship between MCP servers and their Docker containers.
    This is the PRIMARY table for tracking deployed containers.
    """
    __tablename__ = "docker_deployments"

    # Primary Key
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this deployment belongs to"
    )

    # Deployment name for tracking
    deployment_name = Column(
        String(255),
        nullable=False,
        comment="Unique deployment name within tenant"
    )

    # Relationships - MCP server is OPTIONAL (can be linked later)
    mcp_server_id = Column(
        String(255),
        ForeignKey("mcp_servers.id", ondelete="SET NULL"),
        nullable=True,
        comment="MCP server this deployment is for (optional)"
    )

    # Store MCP server name for display even if link is removed
    mcp_server_name = Column(
        String(255),
        nullable=True,
        comment="MCP server name (cached)"
    )

    # Image tracking - OPTIONAL (container may use pre-existing image not in docker_images table)
    image_id = Column(
        Integer,
        ForeignKey("docker_images.id", ondelete="SET NULL"),
        nullable=True,
        comment="Docker image used for this deployment (optional)"
    )

    # Store image name directly for display (independent of docker_images table)
    image_name = Column(
        String(600),
        nullable=False,
        comment="Full image name used for deployment"
    )

    # Container information
    container_id = Column(
        String(64),
        nullable=False,
        comment="Docker container ID"
    )

    container_name = Column(
        String(255),
        nullable=False,
        comment="Docker container name"
    )

    # GitHub information if built from GitHub
    github_repo = Column(
        String(500),
        nullable=True,
        comment="GitHub repository URL if deployed from GitHub"
    )

    # Network configuration
    network = Column(
        String(255),
        nullable=False,
        default="mcp-gateway-network",
        comment="Docker network"
    )

    host_port = Column(
        Integer,
        nullable=True,
        comment="Host port bound to container"
    )

    internal_port = Column(
        Integer,
        nullable=False,
        default=8000,
        comment="Internal container port"
    )

    # Container configuration
    environment = Column(
        JSON,
        nullable=True,
        comment="Environment variables"
    )

    volumes = Column(
        JSON,
        nullable=True,
        comment="Volume mounts"
    )

    memory_limit = Column(
        String(50),
        nullable=False,
        default="512m",
        comment="Memory limit"
    )

    cpu_limit = Column(
        Float,
        nullable=False,
        default=1.0,
        comment="CPU limit"
    )

    restart_policy = Column(
        String(50),
        nullable=False,
        default="unless-stopped",
        comment="Restart policy"
    )

    # Deployment metadata
    deployed_by = Column(
        String(50),
        nullable=True,
        comment="Admin user who deployed"
    )

    deployed_at = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="When deployment was created"
    )

    # Status tracking
    status = Column(
        String(50),
        nullable=False,
        default="running",
        comment="Deployment status: running, stopped, failed, removed"
    )

    last_status_check = Column(
        DateTime,
        nullable=True,
        comment="Last time status was checked"
    )

    last_started_at = Column(
        DateTime,
        nullable=True,
        comment="When container last started"
    )

    last_stopped_at = Column(
        DateTime,
        nullable=True,
        comment="When container last stopped"
    )

    # Health tracking
    health_status = Column(
        String(50),
        nullable=True,
        comment="Container health status"
    )

    consecutive_failures = Column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of consecutive health check failures"
    )

    # Statistics (updated periodically)
    cpu_usage_percent = Column(
        Float,
        nullable=True,
        comment="Current CPU usage percentage"
    )

    memory_usage_mb = Column(
        Float,
        nullable=True,
        comment="Current memory usage in MB"
    )

    network_rx_mb = Column(
        Float,
        nullable=True,
        comment="Total network received in MB"
    )

    network_tx_mb = Column(
        Float,
        nullable=True,
        comment="Total network transmitted in MB"
    )

    stats_updated_at = Column(
        DateTime,
        nullable=True,
        comment="When stats were last updated"
    )

    # Relationships
    mcp_server = relationship("MCPServer", back_populates="docker_deployment")
    image = relationship("DockerImage", back_populates="deployments")

    __table_args__ = (
        # Deployment names, container IDs, and names should be unique within tenant
        UniqueConstraint('tenant_id', 'deployment_name', name='uix_docker_deployments_tenant_name'),
        UniqueConstraint('tenant_id', 'container_id', name='uix_docker_deployments_tenant_container_id'),
        UniqueConstraint('tenant_id', 'container_name', name='uix_docker_deployments_tenant_container_name'),

        # Primary queries - "Get all running deployments for tenant"
        Index('ix_docker_deployments_tenant_status', 'tenant_id', 'status'),

        # Deployment lookup by name within tenant
        Index('ix_docker_deployments_tenant_name', 'tenant_id', 'deployment_name'),

        # Server deployments - "Get deployment for MCP server"
        Index('ix_docker_deployments_tenant_server', 'tenant_id', 'mcp_server_id'),

        # Image usage tracking - "Find deployments using specific image"
        Index('ix_docker_deployments_tenant_image', 'tenant_id', 'image_id'),

        # Health monitoring - "Find unhealthy deployments"
        Index('ix_docker_deployments_tenant_health', 'tenant_id', 'health_status', 'consecutive_failures'),

        # GitHub source tracking
        Index('ix_docker_deployments_tenant_github', 'tenant_id', 'github_repo'),

        # Deployment history
        Index('ix_docker_deployments_tenant_deployed', 'tenant_id', 'deployed_at'),

        # Status timeline - "Show recently stopped deployments"
        Index('ix_docker_deployments_tenant_stopped', 'tenant_id', 'last_stopped_at'),

        {
            'comment': 'Docker container deployments - primary tracking table'
        }
    )

    def __repr__(self) -> str:
        return (
            f"<DockerDeployment(id={self.id}, name='{self.deployment_name}', "
            f"container='{self.container_name}', status='{self.status}')>"
        )

    def is_running(self) -> bool:
        """Check if deployment is running"""
        return self.status == "running"

    def has_mcp_server(self) -> bool:
        """Check if deployment is linked to an MCP server"""
        return self.mcp_server_id is not None

    def link_mcp_server(self, mcp_server_id: str, mcp_server_name: str) -> None:
        """Link this deployment to an MCP server"""
        self.mcp_server_id = mcp_server_id
        self.mcp_server_name = mcp_server_name

    def unlink_mcp_server(self) -> None:
        """Unlink MCP server from this deployment"""
        self.mcp_server_id = None
        # Keep mcp_server_name for history

    def update_stats(
            self,
            cpu_percent: float,
            memory_mb: float,
            network_rx_mb: float,
            network_tx_mb: float
    ) -> None:
        """Update deployment statistics"""
        self.cpu_usage_percent = cpu_percent
        self.memory_usage_mb = memory_mb
        self.network_rx_mb = network_rx_mb
        self.network_tx_mb = network_tx_mb
        self.stats_updated_at = datetime.now(timezone.utc)

    def update_status(
            self,
            status: str,
            health_status: Optional[str] = None
    ) -> None:
        """Update deployment status"""
        old_status = self.status
        self.status = status
        self.last_status_check = datetime.now(timezone.utc)

        if health_status:
            self.health_status = health_status

        # Update timestamps
        if status == "running" and old_status != "running":
            self.last_started_at = datetime.now(timezone.utc)
            self.consecutive_failures = 0
        elif status == "stopped" and old_status == "running":
            self.last_stopped_at = datetime.now(timezone.utc)
        elif status == "failed":
            self.consecutive_failures += 1

    def to_dict(self, include_stats: bool = True) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        data = {
            "id": self.id,
            "deployment_name": self.deployment_name,
            "mcp_server_id": self.mcp_server_id,
            "mcp_server_name": self.mcp_server_name,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "image": self.image_name,  # Use stored image name
            "image_id": self.image_id,  # Optional - may be None
            "github_repo": self.github_repo,
            "network": self.network,
            "host_port": self.host_port,
            "internal_port": self.internal_port,
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "restart_policy": self.restart_policy,
            "deployed_by": self.deployed_by,
            "deployed_at": self.deployed_at.isoformat() if self.deployed_at else None,
            "status": self.status,
            "health_status": self.health_status,
            "last_started_at": self.last_started_at.isoformat() if self.last_started_at else None,
            "last_stopped_at": self.last_stopped_at.isoformat() if self.last_stopped_at else None,
            "consecutive_failures": self.consecutive_failures
        }

        if include_stats and self.stats_updated_at:
            data["stats"] = {
                "cpu_usage_percent": self.cpu_usage_percent,
                "memory_usage_mb": self.memory_usage_mb,
                "network_rx_mb": self.network_rx_mb,
                "network_tx_mb": self.network_tx_mb,
                "updated_at": self.stats_updated_at.isoformat()
            }

        return data