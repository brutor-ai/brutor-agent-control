# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Kubernetes Deployment Database Models
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base


class KubernetesDeployment(Base):
    """Kubernetes deployment record"""
    __tablename__ = "kubernetes_deployments"

    # Primary fields
    id = Column(String(255), primary_key=True)  # dpl-ULID format
    tenant_id = Column(String(255), ForeignKey("tenants.id"), nullable=False, index=True)
    deployment_name = Column(String(255), nullable=False, index=True)
    namespace = Column(String(255), default="default", nullable=False)

    # MCP Server linkage (optional)
    mcp_server_id = Column(String(255), ForeignKey("mcp_servers.id"), nullable=True)
    mcp_server_name = Column(String(255), nullable=True)

    # Image information
    image_id = Column(Integer, ForeignKey("docker_images.id"), nullable=True)
    image_name = Column(String(512), nullable=True)

    # Kubernetes resource identifiers
    deployment_uid = Column(String(255), nullable=True)  # K8s deployment UID
    pod_selector = Column(String(255), nullable=True)  # Label selector for pods
    service_name = Column(String(255), nullable=True)  # K8s service name

    # Configuration
    replicas = Column(Integer, default=1, nullable=False)
    container_port = Column(Integer, default=8000, nullable=False)
    service_port = Column(Integer, nullable=True)  # External port (NodePort/LoadBalancer)
    service_type = Column(String(50), default="ClusterIP", nullable=False)  # ClusterIP, NodePort, LoadBalancer

    # Resource limits
    cpu_request = Column(String(50), default="100m")  # e.g., "100m", "0.5", "1"
    cpu_limit = Column(String(50), default="1")
    memory_request = Column(String(50), default="128Mi")  # e.g., "128Mi", "512Mi", "1Gi"
    memory_limit = Column(String(50), default="512Mi")

    # Source information
    github_repo = Column(String(512), nullable=True)

    # Deployment metadata
    deployed_by = Column(String(255), nullable=True)
    deployed_at = Column(DateTime(timezone=True), nullable=False)

    # Status tracking
    status = Column(String(50), default="pending", nullable=False)  # pending, running, failed, stopped
    last_status_check = Column(DateTime(timezone=True), nullable=True)

    # Health status
    health_status = Column(String(50), nullable=True)  # healthy, unhealthy, unknown
    ready_replicas = Column(Integer, default=0)
    available_replicas = Column(Integer, default=0)

    # Statistics
    restart_count = Column(Integer, default=0)
    last_restart_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    mcp_server = relationship("MCPServer", back_populates="kubernetes_deployment")
    image = relationship("DockerImage", back_populates="kubernetes_deployments")

    def __repr__(self):
        return f"<KubernetesDeployment(id={self.id}, name={self.deployment_name}, namespace={self.namespace}, status={self.status})>"

    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "deployment_name": self.deployment_name,
            "namespace": self.namespace,
            "mcp_server_id": self.mcp_server_id,
            "mcp_server_name": self.mcp_server_name,
            "image_name": self.image_name,
            "deployment_uid": self.deployment_uid,
            "service_name": self.service_name,
            "replicas": self.replicas,
            "ready_replicas": self.ready_replicas,
            "available_replicas": self.available_replicas,
            "container_port": self.container_port,
            "service_port": self.service_port,
            "service_type": self.service_type,
            "cpu_request": self.cpu_request,
            "cpu_limit": self.cpu_limit,
            "memory_request": self.memory_request,
            "memory_limit": self.memory_limit,
            "github_repo": self.github_repo,
            "deployed_by": self.deployed_by,
            "deployed_at": self.deployed_at.isoformat() if self.deployed_at else None,
            "status": self.status,
            "health_status": self.health_status,
            "last_status_check": self.last_status_check.isoformat() if self.last_status_check else None,
            "restart_count": self.restart_count,
            "last_restart_at": self.last_restart_at.isoformat() if self.last_restart_at else None
        }


class KubernetesCluster(Base):
    """Kubernetes cluster configuration"""
    __tablename__ = "kubernetes_clusters"

    id = Column(String(255), primary_key=True)
    tenant_id = Column(String(255), ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    context_name = Column(String(255), nullable=False)  # kubectl context name
    api_server = Column(String(512), nullable=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "context_name": self.context_name,
            "api_server": self.api_server,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }