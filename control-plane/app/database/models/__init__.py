# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Central export for all database models
"""

from app.database.models.tenant import Tenant
from app.database.models.admin import (
    SystemAdmin,
    TenantAdmin,
    TenantRole,
    TenantRolePermission,
    SystemAdminAuditLog,
    TenantAdminAuditLog,
    SystemAdminSession,
    TenantAdminSession
)
from app.database.models.admin_stream_ticket import AdminStreamTicket
from app.database.models.end_user import EndUser
from app.database.models.end_user_revoked_token import EndUserRevokedToken
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group import ResourceGroup, resource_group_mcp_capabilities
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.usage_log import UsageLog
from app.database.models.guardrail_config import GuardrailConfig
from app.database.models.argument_policy import (
    ArgumentPolicy,
    resource_group_argument_policies,
)
from app.database.models.semantic_cache_config import SemanticCacheConfig, resource_group_cache_configs
from app.database.models.router_cache_config import RouterCacheConfig
from app.database.models.semantic_cache_tool_rule import SemanticCacheToolRule
from app.database.models.prompt_cache_policy import (
    PromptCachePolicy,
    resource_group_prompt_cache_policies,
)
from app.database.models.guardrail_break_glass import GuardrailBreakGlass
from app.database.models.observed_agent_event import ObservedAgentEvent
from app.database.models.usage_alert import UsageAlert
from app.database.models.system_alert import SystemAlert
from app.database.models.notification_channel import (
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from app.database.models.assurance_inbox import (
    AssuranceInboxEvent,
    AssuranceInboxItem,
)
from app.database.models.audit_chain_prune import AuditChainPrune
from app.database.models.ai_system_lifecycle_event import AISystemLifecycleEvent
from app.database.models.data_erasure import DataErasureEvent
from app.database.models.assurance_check import (
    AssuranceCheck,
    AssuranceCheckResult,
    AssuranceCheckVersion,
    AssuranceDigest,
)
from app.database.models.mcp_server import MCPServer
from app.database.models.mcp_capability_snapshot import MCPCapabilitySnapshot
from app.database.models.virtual_server import VirtualServer
from app.database.models.idp_server import IdPServer
from app.database.models.scim_token import ScimToken
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.server_config import ServerConfig
from app.database.models.proxy_log import ProxyLog
from app.database.models.proxy_metric import ProxyMetricEntry
from app.database.models.audit_chain_checkpoint import AuditChainCheckpoint
from app.database.models.audit_export_destination import AuditExportDestination
from app.database.models.saved_log_view import (
    SavedLogView,
    SCOPE_PROXY_LOGS as SAVED_LOG_VIEW_SCOPE_PROXY_LOGS,
    SCOPE_ADMIN_AUDIT_LOGS as SAVED_LOG_VIEW_SCOPE_ADMIN_AUDIT_LOGS,
    VALID_SCOPES as SAVED_LOG_VIEW_VALID_SCOPES,
)
# System Operations Console. The audit table + the
# scheduler pause/resume state.
from app.database.models.system_operation_log import (
    SystemOperationLog,
    VALID_ACTOR_TYPES as SYSTEM_OPERATION_LOG_VALID_ACTOR_TYPES,
    ACTOR_TYPE_SYSTEM_ADMIN as SYSTEM_OPERATION_LOG_ACTOR_TYPE_SYSTEM_ADMIN,
    ACTOR_TYPE_TENANT_ADMIN as SYSTEM_OPERATION_LOG_ACTOR_TYPE_TENANT_ADMIN,
)
from app.database.models.scheduler_runtime_state import (
    SchedulerRuntimeState,
    KNOWN_SCHEDULERS,
)
from app.database.models.mcp_registry import MCPRegistry
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.models.oauth_token import OAuthToken
from app.database.models.oauth_cimd_client import OAuthCimdClient
from app.database.models.docker_deploy import DockerImage, DockerDeployment
from app.database.models.kubernetes_deploy import KubernetesDeployment, KubernetesCluster
from app.database.models.ecs_deploy import EcsDeployment
from app.database.models.llm_model import LLModel
from app.database.models.llm_catalog import (
    LlmCatalog,
    CATALOG_MODES,
    CATALOG_SOURCES,
    seeded_catalog_id,
    slug_model_name,
)
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import (
    LlmProviderCatalog,
    PROVIDER_CATALOG_SOURCES,
    PROVIDER_CATALOG_SOURCE_BUILTIN,
    PROVIDER_CATALOG_SOURCE_CUSTOM,
    seeded_provider_catalog_id,
)
from app.database.models.usage_import import (
    UsageImportCheckpoint,
    UsageImportRunLog,
)
from app.database.models.llm_routing_group import LLMRoutingGroup, LLMRoutingGroupTier
from app.database.models.llm_runner_machine import LLMRunnerMachine
from app.database.models.api_key import APIKey
from app.database.models.gateway_config import HealthCheckConfig
from app.database.models.skill_source import SkillSource
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.agent_skill_resource import AgentSkillResource
from app.database.models.agent_skill_script import AgentSkillScript
from app.database.models.agent_skill_asset import AgentSkillAsset
from app.database.models.agent_skill_execution import AgentSkillExecution
from app.database.models.agent_skill_dependency import AgentSkillDependency
from app.database.models.batch_job import BatchJob
from app.database.models.batch_request import BatchRequest
from app.database.models.policy_bundle import PolicyBundle, PolicyBundleEvent, PolicyTemplate
from app.database.models.tool_approval_request import ToolApprovalRequest
from app.database.models.kb_collection import KbCollection, resource_group_kb_collections
from app.database.models.kb_document import KbDocument
from app.database.models.kb_chunk import KbChunk
from app.database.models.kb_connector import (
    ALLOWED_CONNECTOR_TYPES,
    ALLOWED_CRED_KINDS,
    ALLOWED_RUN_STATUSES,
    ALLOWED_RUN_TRIGGERS,
    CONNECTOR_TYPE_CONFLUENCE,
    CONNECTOR_TYPE_GITHUB,
    CONNECTOR_TYPE_GOOGLE_DRIVE,
    CONNECTOR_TYPE_JIRA,
    CONNECTOR_TYPE_NOTION,
    CONNECTOR_TYPE_SHAREPOINT,
    CONNECTOR_TYPE_SLACK,
    CONNECTOR_TYPE_WEB_CRAWLER,
    CREDENTIALLESS_CONNECTOR_TYPES,
    CRED_KIND_API_KEY,
    CRED_KIND_OAUTH,
    CRED_KIND_SERVICE_ACCOUNT,
    KbConnector,
    KbConnectorCredential,
    KbConnectorCheckpoint,
    KbConnectorRunLog,
    RUN_STATUS_FAILURE,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCESS,
    RUN_TRIGGER_INITIAL,
    RUN_TRIGGER_MANUAL,
    RUN_TRIGGER_SCHEDULE,
    resource_group_kb_connectors,
)
from app.database.models.kb_connector_identity import (
    ALLOWED_IDENTITY_CONNECTOR_TYPES,
    ALLOWED_MAPPING_SOURCES,
    KbConnectorIdentityMapping,
    MAPPING_SOURCE_MANUAL,
    MAPPING_SOURCE_OIDC_CLAIM,
    MAPPING_SOURCE_SCIM,
    MAPPING_SOURCE_SELF_LINK,
)
from app.database.models.media_file import MediaFile
from app.database.models.video_generation_job import VideoGenerationJob
from app.database.models.notification import Notification
from app.database.models.search_provider_config import SearchProviderConfig
from app.database.models.schema_version import SchemaVersion
from app.database.models.edition_marker import EditionMarker
from app.database.models.agent_card import (
    AgentCard,
    TRUST_TIER_UNSIGNED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_PARTNER_SIGNED,
    TRUST_TIER_REVOKED,
    VALID_TRUST_TIERS,
)
from app.database.models.agent_card_capability import (
    AgentCardCapability,
    HANDLER_TYPE_NOOP,
    HANDLER_TYPE_SKILL,
    VALID_HANDLER_TYPES,
)
from app.database.models.tenant_a2a_key import TenantA2AKey, KEY_ALGORITHM_ED25519
from app.database.models.a2a_task import A2aTask, A2aPushNotificationConfig
from app.database.models.ai_asset import (
    AiAsset,
    ASSET_KINDS,
    LIFECYCLE_STAGES,
    RISK_TIERS,
    ENFORCEMENT_MODES,
    STATUSES as AI_ASSET_STATUSES,
    SOURCES as AI_ASSET_SOURCES,
)
from app.database.models.ai_asset_factsheet import AiAssetFactsheet
from app.database.models.discovery import (
    CollectorKey,
    DiscoveredAsset,
    DiscoveryEvidence,
    DiscoveryEvent,
    ASSET_TYPES as DISCOVERY_ASSET_TYPES,
    EVIDENCE_TYPES as DISCOVERY_EVIDENCE_TYPES,
    TRUST_TIERS as DISCOVERY_TRUST_TIERS,
    GOVERNANCE_STATUSES as DISCOVERY_GOVERNANCE_STATUSES,
)
from app.database.models.iso42001 import (
    Iso42001Control,
    Iso42001ControlCoverage,
    COVERAGE_STATUSES as ISO42001_COVERAGE_STATUSES,
)
# Shared AI Resource fields — version + authorship. Imported
# explicitly rather than relied on transitively: importing this module is
# what installs the flush listener that stamps created_by / updated_by, so
# the import must be deterministic, not a side effect of whichever model
# happened to load first.
from app.database.models.resource_mixins import (
    DEFAULT_RESOURCE_VERSION,
    ResourceAuthorshipMixin,
    ResourceVersionMixin,
    SYSTEM_ACTOR,
    acting_as,
    set_current_actor,
)
# Health score + the Assurance Report
from app.database.models.ai_system_health import (
    AISystemAssuranceReport,
    AISystemHealth,
    HEALTH_STATES,
    NON_GREEN_STATES,
    REPORT_TRIGGERS,
    score_health,
)
# Shadow replay + the closed loop
from app.database.models.ai_system_replay import (
    AISystemReplayCase,
    AISystemReplayRun,
    AISystemReplaySuite,
    PASSING_VERDICTS,
    REPLAY_STATUSES,
    SIDE_EFFECT_POLICIES,
    VERDICTS,
    summarize,
)
from app.database.models.ai_system_data_scope import (
    AISystemDataScope,
    AISystemReachRule,
    AISystemRecordAccess,
    AISystemRunScope,
)
from app.database.models.ai_system_response_policy import (
    ACTION_KINDS,
    AISystemResponsePolicy,
    SEVERITY_RANK,
    SIGNAL_KINDS,
    PolicyValidationError,
    validate_actions,
)
# Hash-pinned contracts + autonomy
from app.database.models.ai_system_contract import (
    AISystemContract,
    AUTONOMY_LEVELS,
    AUTONOMY_RANK,
    CONTRACT_STATUSES,
    DEFAULT_AUTONOMY_LEVEL,
    canonical_json,
    hash_closure,
    resolve_autonomy,
)
# External assurance evidence — impact assessments, eval / red-team /
# pen-test reports attached to an AI System
from app.database.models.ai_system_evidence import (
    AISystemEvidence,
    EVIDENCE_KINDS,
    EVIDENCE_IMPACT_ASSESSMENT,
)
# Baselines + drift detection
from app.database.models.ai_system_drift import (
    AISystemBaseline,
    AISystemDriftEvent,
    DRIFT_CLASSES,
    DRIFT_STATUSES,
    CAUSE_LADDER,
    MIN_RUNS_FOR_BASELINE,
)
# Liveness expectations (the only signal that fires on
# the ABSENCE of traffic)
from app.database.models.ai_system_liveness import (
    AISystemLivenessExpectation,
    LIVENESS_MODES,
    SILENCE_ALERTING_MODES,
    DEFAULT_MODE_BY_SYSTEM_KIND,
    LIVENESS_DETECTORS,
    MIN_RUNS_TO_LEARN_CADENCE,
)
# The run ledger (keystone of AI System lifecycle assurance)
from app.database.models.ai_system_run import (
    AISystemRun,
    TERMINAL_STATES,
    FINALIZED_BY,
    CHAIN_INTEGRITIES,
    CHAIN_INTACT,
    CHAIN_PARTIAL,
    CHAIN_BROKEN,
    MAX_REOPENS_BEFORE_FRAGMENTED,
    DEFAULT_RUN_IDLE_TIMEOUT_SECONDS,
)
# Portal thumbs, the run ledger's only true-outcome signal
from app.database.models.ai_system_run_feedback import AISystemRunFeedback
# Composite AI Systems: declared dependency edges
# and the per-system run segments the core ledger writes.
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_run_segment import AISystemRunSegment
# Baseline epochs and incident windows: resolutions the
# detector honours.
from app.database.models.ai_system_drift_resolution import (
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
    AISystemIncidentWindowFinding,
)
# Platform measurement changes applied on this installation.
from app.database.models.platform_measurement_change import PlatformMeasurementChange
# Declared here as well as in its migration: a migration-only table would be missing on a
# fresh database, which is stamped rather than migrated.
from app.database.models.session_budget_reset import SessionBudgetReset
from app.database.models.run_intervention import (
    RunIntervention,
    ACTION_ABORT,
    STATUS_REQUESTED,
    STATUS_EFFECTIVE,
    STATUS_CLOSED,
)  # noqa: F401 — the vocabulary travels with the model; see __all__ below
from app.database.models.agent_identity_approval_history import (
    AgentIdentityApprovalHistory,
)
from app.database.models.agent_identity import (
    AgentIdentity,
    AgentGrant,
    ResourceGroupAgentMember,
    AGENT_SOURCES,
    AGENT_STATUSES,
    ENFORCEMENT_MODES,
    GRANT_ACTION_TYPES,
    GRANT_EFFECTS,
)
# Observed releases of an agent identity's software.
from app.database.models.agent_implementation_release import AgentImplementationRelease
from app.database.models.semantic_policy import (
    SemanticPolicy,
    resource_group_semantic_policies,
    POLICY_ACTIONS,
    POLICY_MODES,
    POLICY_SURFACES,
)
# The compliance evidence programme. Records, logs,
# heads and receipts are core-written; witnesses, the framework registry,
# compliance profiles and disclosure records are control-plane-written.
from app.database.models.action_record import (
    ActionRecord,
    RECORD_KINDS,
    RECORD_VERDICTS,
)
from app.database.models.evidence_log import (
    EvidenceLog,
    EvidenceLogNode,
    EvidenceLogEntry,
    EvidenceTreeHead,
    EvidenceReceipt,
    ENTRY_KINDS,
    HEAD_GRADES,
    WITNESS_KINDS,
    RECEIPT_VERDICTS,
)
from app.database.models.witness import Witness, WITNESS_SCOPES
from app.database.models.disclosure_record import (
    DisclosureRecord,
    COMPLETENESS_VALUES,
)
from app.database.models.compliance_framework import (
    ComplianceFramework,
    FRAMEWORK_SEED,
    FRAMEWORK_KINDS,
)
from app.database.models.ai_system_compliance_profile import (
    AISystemComplianceProfile,
    OPERATOR_ROLES,
)
# Evidence programme: bundles and signing keys are core-written;
# settings, calibration, reports, incidents, notice configs and
# documentation exports are control-plane-written.
from app.database.models.evidence_bundle import EvidenceBundle, BUNDLE_SELECTIONS
from app.database.models.evidence_signing_key import EvidenceSigningKey, KEY_STATUSES
from app.database.models.evidence_settings import EvidenceSettings
from app.database.models.platform_settings import PlatformSettings
from app.database.models.assurance_check_calibration import (
    AssuranceCheckRubric,
    AssuranceCheckReview,
    REVIEW_RESULTS,
    REVIEW_STATUSES,
)
from app.database.models.compliance_report import (
    ComplianceReport,
    REPORT_CADENCES,
    REPORT_STATUSES,
)
from app.database.models.compliance_incident import (
    ComplianceIncident,
    ComplianceIncidentEvent,
    INCIDENT_STATUSES,
    INCIDENT_EVENTS,
)
from app.database.models.compliance_notice_config import (
    ComplianceNoticeConfig,
    NOTICE_SURFACES,
)
from app.database.models.compliance_documentation_export import (
    ComplianceDocumentationExport,
)


__all__ = [
    "AdminStreamTicket",
    # Compliance evidence programme
    "ActionRecord",
    "RECORD_KINDS",
    "RECORD_VERDICTS",
    "EvidenceLog",
    "EvidenceLogNode",
    "EvidenceLogEntry",
    "EvidenceTreeHead",
    "EvidenceReceipt",
    "ENTRY_KINDS",
    "HEAD_GRADES",
    "WITNESS_KINDS",
    "RECEIPT_VERDICTS",
    "Witness",
    "WITNESS_SCOPES",
    "DisclosureRecord",
    "COMPLETENESS_VALUES",
    "ComplianceFramework",
    "FRAMEWORK_SEED",
    "FRAMEWORK_KINDS",
    "AISystemComplianceProfile",
    "OPERATOR_ROLES",
    # Evidence programme: bundles, keys, settings, reports, incidents
    "EvidenceBundle",
    "BUNDLE_SELECTIONS",
    "EvidenceSigningKey",
    "KEY_STATUSES",
    "EvidenceSettings",
    "PlatformSettings",
    "AssuranceCheckRubric",
    "AssuranceCheckReview",
    "REVIEW_RESULTS",
    "REVIEW_STATUSES",
    "ComplianceReport",
    "REPORT_CADENCES",
    "REPORT_STATUSES",
    "ComplianceIncident",
    "ComplianceIncidentEvent",
    "INCIDENT_STATUSES",
    "INCIDENT_EVENTS",
    "ComplianceNoticeConfig",
    "NOTICE_SURFACES",
    "ComplianceDocumentationExport",
    "RunIntervention",
    "AISystemDependency",
    "AISystemBaselineEpoch",
    "AISystemIncidentWindow",
    "AISystemIncidentWindowFinding",
    "PlatformMeasurementChange",
    "AISystemRunSegment",
    "AgentImplementationRelease",
    # The intervention vocabulary. Exported alongside the
    # model because callers that read `action` or `status` should not have to
    # reach past the package for the values they compare against.
    "ACTION_ABORT",
    "STATUS_REQUESTED",
    "STATUS_EFFECTIVE",
    "STATUS_CLOSED",
    "AuditChainPrune",
    "AISystemLifecycleEvent",
    "DataErasureEvent",
    "AISystemEvidence",
    "EVIDENCE_KINDS",
    "EVIDENCE_IMPACT_ASSESSMENT",
    'Tenant',

    # Admin users
    'SystemAdmin',
    'SystemAdminSession',
    'SystemAdminAuditLog',
    'TenantAdmin',
    'TenantAdminSession',
    'TenantAdminAuditLog',

    # Tenant roles (RBAC)
    'TenantRole',
    'TenantRolePermission',

    # Guardrails - content filters
    'GuardrailConfig',

    # Argument Policies (POLICY-10 — AST-aware tool/skill arg checks)
    'ArgumentPolicy',
    'resource_group_argument_policies',

    # Semantic Cache (gateway-side response cache)
    'SemanticCacheConfig',
    'resource_group_cache_configs',
    'SemanticCacheToolRule',

    # Routing-group in-process micro-cache (separate from
    # SemanticCacheConfig — see model docstring).
    'RouterCacheConfig',

    # Prompt Cache (provider-side)
    'PromptCachePolicy',
    'resource_group_prompt_cache_policies',

    # End Users
    'EndUser',
    'EndUserGroup',
    'end_user_group_members',
    'resource_group_end_user_groups',

    # Resource Groups
    'ResourceGroup',
    'resource_group_mcp_capabilities',
    'ResourceGroupMembership',
    'UsageLog',
    'GuardrailBreakGlass',
    'ObservedAgentEvent',
    'UsageAlert',
    'SystemAlert',

    # Agent Skills
    'SkillSource',
    'AgentSkill',
    'resource_group_agent_skills',
    'AgentSkillResource',
    'AgentSkillScript',
    'AgentSkillAsset',
    'AgentSkillExecution',
    'AgentSkillDependency',

    # LLM Server and Router
    'LLModel',
    'LlmCatalog',
    'CATALOG_MODES',
    'CATALOG_SOURCES',
    'seeded_catalog_id',
    'slug_model_name',
    'LlmProvider',
    'UsageImportCheckpoint',
    'UsageImportRunLog',
    'LlmProviderCatalog',
    'PROVIDER_CATALOG_SOURCES',
    'PROVIDER_CATALOG_SOURCE_BUILTIN',
    'PROVIDER_CATALOG_SOURCE_CUSTOM',
    'seeded_provider_catalog_id',
    'LLMRoutingGroup',
    'LLMRoutingGroupTier',
    'LLMRunnerMachine',
    'APIKey',

    # MCP Server and Virtual Server
    'MCPServer',
    'VirtualServer',

    # OAuth/Identity provider
    'IdPServer',
    'ScimToken',
    'OAuthAppClient',
    'ServerConfig',
    'OAuthToken',
    'OAuthCimdClient',

    # Deployment
    'DockerImage',
    'DockerDeployment',
    'KubernetesDeployment',
    'KubernetesCluster',
    'EcsDeployment',

    # MCP Registry & Catalog
    'MCPRegistry',
    'MCPCatalogEntry',

    # Batch Processing
    'BatchJob',
    'BatchRequest',

    # Proxy Logging/Metrics
    'ProxyLog',
    'ProxyMetricEntry',

    'HealthCheckConfig',

    # Policy-as-Code
    'PolicyBundle',
    'PolicyBundleEvent',
    'PolicyTemplate',

    # Tool Approvals
    'ToolApprovalRequest',

    # Knowledge Base (RAG)
    'KbCollection',
    'resource_group_kb_collections',
    'KbDocument',
    'KbChunk',
    # KB Connectors (pull-ingest from Confluence/Notion/GDrive/Slack/GitHub)
    'KbConnector',
    'KbConnectorCredential',
    'KbConnectorCheckpoint',
    'KbConnectorRunLog',
    'resource_group_kb_connectors',
    'ALLOWED_CONNECTOR_TYPES',
    'ALLOWED_CRED_KINDS',
    'ALLOWED_RUN_STATUSES',
    'ALLOWED_RUN_TRIGGERS',
    'CONNECTOR_TYPE_CONFLUENCE',
    'CONNECTOR_TYPE_GITHUB',
    'CONNECTOR_TYPE_GOOGLE_DRIVE',
    'CONNECTOR_TYPE_JIRA',
    'CONNECTOR_TYPE_NOTION',
    'CONNECTOR_TYPE_SHAREPOINT',
    'CONNECTOR_TYPE_SLACK',
    'CONNECTOR_TYPE_WEB_CRAWLER',
    'CREDENTIALLESS_CONNECTOR_TYPES',
    'CRED_KIND_API_KEY',
    'CRED_KIND_OAUTH',
    'CRED_KIND_SERVICE_ACCOUNT',
    'RUN_STATUS_FAILURE',
    'RUN_STATUS_RUNNING',
    'RUN_STATUS_SUCCESS',
    'RUN_TRIGGER_INITIAL',
    'RUN_TRIGGER_MANUAL',
    'RUN_TRIGGER_SCHEDULE',
    # KB connector → end-user identity mapping (for search-time ACL filter)
    'KbConnectorIdentityMapping',
    'ALLOWED_IDENTITY_CONNECTOR_TYPES',
    'ALLOWED_MAPPING_SOURCES',
    'MAPPING_SOURCE_MANUAL',
    'MAPPING_SOURCE_OIDC_CLAIM',
    'MAPPING_SOURCE_SCIM',
    'MAPPING_SOURCE_SELF_LINK',

    # Media files (multimodal Files API)
    'MediaFile',

    # Video generation async pipeline
    'VideoGenerationJob',

    # Notifications
    'Notification',

    # Search Provider Configuration
    'SearchProviderConfig',

    # Schema Version
    'SchemaVersion',
    'EditionMarker',

    # MCP capability snapshots (historical tools/resources/prompts)
    'MCPCapabilitySnapshot',

    # Audit export destinations (S3 / SFTP / webhook sinks for audit logs)
    'AuditChainCheckpoint',
    'AuditExportDestination',

    # Saved log views — per-tenant named filter presets for the proxy
    # log + admin audit log search UIs.
    'SavedLogView',
    'SAVED_LOG_VIEW_SCOPE_PROXY_LOGS',
    'SAVED_LOG_VIEW_SCOPE_ADMIN_AUDIT_LOGS',
    'SAVED_LOG_VIEW_VALID_SCOPES',

    # System Operations Console. The audit table for
    # operator actions taken from the System page, plus per-scheduler
    # pause/resume state.
    'SystemOperationLog',
    'SYSTEM_OPERATION_LOG_VALID_ACTOR_TYPES',
    'SYSTEM_OPERATION_LOG_ACTOR_TYPE_SYSTEM_ADMIN',
    'SYSTEM_OPERATION_LOG_ACTOR_TYPE_TENANT_ADMIN',
    'SchedulerRuntimeState',
    'KNOWN_SCHEDULERS',

    # A2A (Agent-to-Agent)
    'AgentCard',
    'AgentCardCapability',
    'TenantA2AKey',
    'TRUST_TIER_UNSIGNED',
    'TRUST_TIER_TENANT_SIGNED',
    'TRUST_TIER_PARTNER_SIGNED',
    'TRUST_TIER_REVOKED',
    'VALID_TRUST_TIERS',
    'KEY_ALGORITHM_ED25519',

    # A2A handler binding
    'HANDLER_TYPE_SKILL',
    'HANDLER_TYPE_NOOP',
    'VALID_HANDLER_TYPES',

    # A2A v1.0 — stateful task surface
    'A2aTask',
    'A2aPushNotificationConfig',

    # AI Asset Registry
    'AiAsset',
    'AiAssetFactsheet',
    'ASSET_KINDS',
    'LIFECYCLE_STAGES',
    'RISK_TIERS',
    'ENFORCEMENT_MODES',
    'AI_ASSET_STATUSES',
    'AI_ASSET_SOURCES',

    # Discovery plane — out-of-band AI-asset discovery (observe-only)
    'CollectorKey',
    'DiscoveredAsset',
    'DiscoveryEvidence',
    'DiscoveryEvent',
    'DISCOVERY_ASSET_TYPES',
    'DISCOVERY_EVIDENCE_TYPES',
    'DISCOVERY_TRUST_TIERS',
    'DISCOVERY_GOVERNANCE_STATUSES',

    # ISO/IEC 42001 AIMS — Annex A control catalog + per-tenant coverage
    'Iso42001Control',
    'Iso42001ControlCoverage',
    'ISO42001_COVERAGE_STATUSES',

    # AI Systems + unified control model

    # Agent Identity — agents as governable principals + runtime authz
    'AgentIdentity',
    'AgentIdentityApprovalHistory',
    'AgentGrant',
    'ResourceGroupAgentMember',
    'AGENT_SOURCES',
    'AGENT_STATUSES',
    'ENFORCEMENT_MODES',
    'GRANT_ACTION_TYPES',
    'GRANT_EFFECTS',

    # Shared AI Resource fields
    'ResourceAuthorshipMixin',
    'ResourceVersionMixin',
    'DEFAULT_RESOURCE_VERSION',
    'SYSTEM_ACTOR',
    'acting_as',
    'set_current_actor',

    # AI System baselines + drift
    'AISystemBaseline',
    'AISystemContract',
    'AISystemAssuranceReport',
    'AISystemHealth',
    'HEALTH_STATES',
    'NON_GREEN_STATES',
    'REPORT_TRIGGERS',
    'score_health',
    'AISystemReplayCase',
    'AISystemReplayRun',
    'AISystemReplaySuite',
    'AISystemResponsePolicy',
    'AISystemDataScope',
    'AISystemRunScope',
    'AISystemRecordAccess',
    'AISystemReachRule',
    'ACTION_KINDS',
    'PASSING_VERDICTS',
    'PolicyValidationError',
    'REPLAY_STATUSES',
    'SEVERITY_RANK',
    'SIDE_EFFECT_POLICIES',
    'SIGNAL_KINDS',
    'VERDICTS',
    'summarize',
    'validate_actions',
    'AISystemDriftEvent',
    'AUTONOMY_LEVELS',
    'AUTONOMY_RANK',
    'CONTRACT_STATUSES',
    'DEFAULT_AUTONOMY_LEVEL',
    'canonical_json',
    'hash_closure',
    'resolve_autonomy',
    'DRIFT_CLASSES',
    'DRIFT_STATUSES',
    'CAUSE_LADDER',
    'MIN_RUNS_FOR_BASELINE',

    # AI System liveness
    'AISystemLivenessExpectation',
    'LIVENESS_MODES',
    'SILENCE_ALERTING_MODES',
    'DEFAULT_MODE_BY_SYSTEM_KIND',
    'LIVENESS_DETECTORS',
    'MIN_RUNS_TO_LEARN_CADENCE',

    # AI System Run ledger
    'AISystemRun',
    'AISystemRunFeedback',
    'SessionBudgetReset',
    'TERMINAL_STATES',
    'FINALIZED_BY',
    'CHAIN_INTEGRITIES',
    'CHAIN_INTACT',
    'CHAIN_PARTIAL',
    'CHAIN_BROKEN',
    'MAX_REOPENS_BEFORE_FRAGMENTED',
    'DEFAULT_RUN_IDLE_TIMEOUT_SECONDS',

    # Semantic Governance Policy — LLM-judged natural-language policy
    'SemanticPolicy',
    'resource_group_semantic_policies',
    'POLICY_ACTIONS',
    'POLICY_MODES',
    'POLICY_SURFACES',
    # Assurance inbox, checks, digest, notifications
    'AssuranceCheck',
    'AssuranceCheckResult',
    'AssuranceCheckVersion',
    'AssuranceDigest',
    'AssuranceInboxEvent',
    'AssuranceInboxItem',
    'NotificationChannel',
    'NotificationDelivery',
    'NotificationSubscription',
    "EndUserRevokedToken",
]