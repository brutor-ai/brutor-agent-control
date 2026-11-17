# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Runner Machine Database Model

Represents a deployment target for local LLM inference:
  - Docker Model Runner (DMR): host:port of a Docker Desktop instance
  - KServe: Kubernetes cluster + namespace with KServe + vLLM runtime

The model is backend-agnostic.  Each record stores connection details and
which adapter type to use.  All backend-specific behaviour lives in the
corresponding LLMDeploymentAdapter implementation.

Architecture:
    AI Gateway ──(adapter_type)──► DMR Host or K8s Cluster
         │                            └── Deployment Backend (DMR / KServe)
         │                                  └── LLM Inference Engine
         │
         └── adapter.test_connection(base_url)
         └── adapter.pull_model(base_url, model_name)
         └── adapter.get_inference_url(base_url, model_name)
"""

from sqlalchemy import Column, String, Integer, JSON, DateTime, Text, ForeignKey, Index, Table, UniqueConstraint
from sqlalchemy.sql import func
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import uuid

from app.database import Base


# Association table for ResourceGroup <-> LLMRunnerMachine
resource_group_deployment_machines = Table(
    'resource_group_deployment_machines',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column('resource_group_id', String(255), ForeignKey('resource_groups.id', ondelete='CASCADE'), nullable=False),
    Column('machine_id', String(100), ForeignKey('llm_runner_machines.id', ondelete='CASCADE'), nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)),
    Column('created_by', String(255)),
    UniqueConstraint('resource_group_id', 'machine_id', name='uq_group_deployment_machine'),
)


class LLMRunnerMachine(Base):
    """
    Remote host configured for local LLM inference.

    Stores connection parameters and the deployment backend type (adapter_type).
    The adapter resolves how to communicate with the host — the ORM model
    itself contains no backend-specific logic.

    Deployed models are queried live from the remote backend (not stored here).
    Resource constraints (e.g. insufficient GPU memory) surface as errors from
    the backend when pulling or running models.
    """

    __tablename__ = "llm_runner_machines"

    # ==================== Primary Key ====================
    id = Column(
        String(100),
        primary_key=True,
        comment="ULID with 'lrm-' prefix"
    )

    # ==================== Multi-tenancy ====================
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this machine belongs to"
    )

    # ==================== Identity ====================
    name = Column(
        String(200),
        nullable=False,
        comment="Display name, e.g. 'Gaming PC RTX 4090'"
    )
    description = Column(
        Text,
        nullable=True,
        comment="Optional description of the machine"
    )

    # ==================== Connection Configuration ====================
    host = Column(
        String(255),
        nullable=False,
        comment="IP address or hostname, e.g. '192.168.1.50'"
    )
    port = Column(
        Integer,
        nullable=False,
        default=12434,
        comment="Deployment backend REST API port"
    )
    adapter_type = Column(
        String(50),
        nullable=False,
        default="dmr",
        comment="Deployment backend type: dmr, kserve, llmd"
    )

    # ==================== KServe / llm-d Configuration ====================
    # Shared between the two Kubernetes adapters — `kserve` deploys the
    # classic `InferenceService` CRD, `llmd` deploys the distributed
    # `LLMInferenceService` CRD. Both use the same kube context,
    # namespace, and HF token secret.
    kube_context = Column(
        String(255),
        nullable=True,
        comment="Kubernetes context name (KServe/llm-d only, None = default/in-cluster)"
    )
    kube_namespace = Column(
        String(255),
        nullable=True,
        default="default",
        comment="Kubernetes namespace for deployed resources (KServe/llm-d only)"
    )
    hf_token_secret = Column(
        String(255),
        nullable=True,
        comment="Name of K8s Secret containing HuggingFace token (for gated models)"
    )

    # ==================== llm-d cluster-wide defaults ====================
    # Only relevant when adapter_type == 'llmd'. Seed per-deployment
    # forms so admins don't re-enter cluster-specific details every
    # time they deploy a model.
    default_prefill_accelerator_type = Column(
        String(50),
        nullable=True,
        comment="Default accelerator for prefill pods (e.g. 'gpu-h100', 'gpu-a100', 'cpu')"
    )
    default_decode_accelerator_type = Column(
        String(50),
        nullable=True,
        comment="Default accelerator for decode pods"
    )
    prefill_nodepool_labels = Column(
        JSON,
        nullable=True,
        comment="Dict of nodepool labels (label → value) seeding nodeSelector on prefill pods"
    )
    decode_nodepool_labels = Column(
        JSON,
        nullable=True,
        comment="Dict of nodepool labels seeding nodeSelector on decode pods"
    )
    kv_cache_pool_config = Column(
        JSON,
        nullable=True,
        comment=(
            "Shared KV cache pool config for llm-d LLMInferenceService: "
            "{ backend: local|redis|p2p, size_gb: int, connection_uri?: str }"
        )
    )
    default_tensor_parallel = Column(
        Integer,
        nullable=True,
        comment="Default tensor-parallelism degree for llm-d deployments"
    )

    # ==================== Status ====================
    status = Column(
        String(20),
        nullable=False,
        default="offline",
        comment="Connection status: online, offline, unreachable, error"
    )
    last_seen = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of last successful connection test"
    )
    last_error = Column(
        Text,
        nullable=True,
        comment="Last connection error message"
    )

    # ==================== Cached Info (JSON) ====================
    system_info = Column(
        JSON,
        nullable=True,
        comment="""
        Cached information from last connection test.
        Structure is adapter-dependent.  Populated by the adapter's
        test_connection() result and stored as-is.
        """
    )

    # ==================== Metadata ====================
    created_by = Column(
        String(255),
        nullable=True,
        comment="Admin ID who created this machine record"
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # ==================== Indexes ====================
    __table_args__ = (
        Index('ix_llm_runner_machine_tenant', 'tenant_id'),
        Index('ix_llm_runner_machine_tenant_status', 'tenant_id', 'status'),
        Index('ix_llm_runner_machine_tenant_name', 'tenant_id', 'name', unique=True),
        Index('ix_llm_runner_machine_tenant_host', 'tenant_id', 'host', 'port'),

        {
            'comment': 'Remote hosts for local LLM inference via deployment adapters'
        }
    )

    # ==================== Helper Properties ====================

    @property
    def base_url(self) -> str:
        """Backend connection string — HTTP URL for DMR, kube context for KServe/llm-d."""
        if self.adapter_type in ("kserve", "llmd"):
            return f"kube:{self.kube_context or 'default'}/{self.kube_namespace or 'default'}"
        return f"http://{self.host}:{self.port}"

    @property
    def is_online(self) -> bool:
        return self.status == "online"

    # ==================== Methods ====================

    def update_status(
        self,
        new_status: str,
        error: Optional[str] = None,
        system_data: Optional[Dict] = None
    ):
        """Update machine status after connection test."""
        self.status = new_status
        self.last_error = error
        self.updated_at = datetime.now(timezone.utc)

        if new_status == "online":
            self.last_seen = datetime.now(timezone.utc)
            self.last_error = None

        if system_data is not None:
            self.system_info = system_data

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "host": self.host,
            "port": self.port,
            "adapter_type": self.adapter_type,
            "kube_context": self.kube_context,
            "kube_namespace": self.kube_namespace,
            "hf_token_secret": self.hf_token_secret,
            # llm-d cluster defaults
            "default_prefill_accelerator_type": self.default_prefill_accelerator_type,
            "default_decode_accelerator_type": self.default_decode_accelerator_type,
            "prefill_nodepool_labels": self.prefill_nodepool_labels,
            "decode_nodepool_labels": self.decode_nodepool_labels,
            "kv_cache_pool_config": self.kv_cache_pool_config,
            "default_tensor_parallel": self.default_tensor_parallel,
            "status": self.status,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "last_error": self.last_error,
            "system_info": self.system_info,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return (
            f"<LLMRunnerMachine(id={self.id}, name={self.name}, "
            f"adapter={self.adapter_type}, host={self.host}:{self.port}, "
            f"status={self.status})>"
        )