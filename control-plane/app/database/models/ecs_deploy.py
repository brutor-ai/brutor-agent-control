# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
ECS / Fargate deployment database model.

Mirrors the structure of `kubernetes_deploy.py` so the admin UI can
treat all three orchestrators (Docker, Kubernetes, ECS) symmetrically.
The fields recorded here are everything the operator needs to manage
the deployment's lifecycle without re-calling AWS — task ARN for
stop, ENI for forensics, image for redeploy.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import relationship

from app.database import Base


class EcsDeployment(Base):
    """ECS / Fargate task record.

    Created when the MCP catalog-import path drives the ECSDriver to
    `run_task`. One row per running task; a redeploy creates a new
    row rather than updating in place so the audit trail of past
    deployments survives.
    """

    __tablename__ = "ecs_deployments"

    # Primary fields
    id = Column(String(255), primary_key=True)  # ecs-ULID
    tenant_id = Column(String(255), ForeignKey("tenants.id"), nullable=False, index=True)
    deployment_name = Column(String(255), nullable=False, index=True)

    # MCP server linkage (optional)
    mcp_server_id = Column(String(255), ForeignKey("mcp_servers.id"), nullable=True)
    mcp_server_name = Column(String(255), nullable=True)

    # ECS identifiers
    cluster_name = Column(String(255), nullable=False)
    task_definition_arn = Column(String(512), nullable=False)
    task_arn = Column(String(512), nullable=False, unique=True)
    region = Column(String(50), nullable=False)

    # Networking — populated once ECS attaches the ENI
    eni_id = Column(String(255), nullable=True)
    private_ip = Column(String(45), nullable=True)  # IPv4 fits in 15, IPv6 up to 45

    # Container info
    image_name = Column(String(512), nullable=False)
    container_port = Column(Integer, nullable=False, default=8000, server_default="8000")

    # Status tracking
    status = Column(String(50), default="pending", nullable=False, server_default="pending")  # pending, running, stopped, failed
    last_status_check = Column(DateTime(timezone=True), nullable=True)
    deployed_at = Column(DateTime(timezone=True), nullable=False)
    stopped_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    mcp_server = relationship("MCPServer", back_populates="ecs_deployment")

    __table_args__ = (
        # The same index the migration creates, so create_all builds it too.
        Index("ix_ecs_deployments_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self):
        return (
            f"<EcsDeployment(id={self.id}, name={self.deployment_name}, "
            f"cluster={self.cluster_name}, status={self.status})>"
        )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "deployment_name": self.deployment_name,
            "mcp_server_id": self.mcp_server_id,
            "mcp_server_name": self.mcp_server_name,
            "cluster_name": self.cluster_name,
            "task_definition_arn": self.task_definition_arn,
            "task_arn": self.task_arn,
            "region": self.region,
            "eni_id": self.eni_id,
            "private_ip": self.private_ip,
            "image_name": self.image_name,
            "container_port": self.container_port,
            "status": self.status,
            "last_status_check": self.last_status_check.isoformat() if self.last_status_check else None,
            "deployed_at": self.deployed_at.isoformat() if self.deployed_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }
