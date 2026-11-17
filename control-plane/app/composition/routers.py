# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The list of routers the control plane mounts.

Routing is first match wins, so the order of routes matters where two routes
can take the same URL. Every such pair lives inside one router today: no URL
is contested between two routers, which makes the order of this list free.
`tests/test_no_shadowed_routes.py` holds both halves: no route is unreachable,
and no pair of routes from two different routers depends on mount order. Keep
it that way: a static path and the parameter route that could swallow it belong
in the same router, static first. Then another distribution can add routers
without having to know where in the order they go.

These are the Community Edition's routers: no entry
here names a feature. The Enterprise Edition's routers are its extension's
(`enterprise/brutor_enterprise/routers.py`), each tagged with its feature
(`RouterSpec.feature`) and mounted after these.

Imports: a factory imported at the top of this module is loaded when
`app.app` is imported; a factory imported inside its build function is loaded
when the application is created. Each router keeps the one it had.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Tuple

from fastapi import APIRouter, FastAPI

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router
from app.api.v1.endpoints.admin.agent_skills_export import create_agent_skills_export_router
from app.api.v1.endpoints.admin.agent_skills_workflow import create_agent_skill_workflows_router
from app.api.v1.endpoints.admin.argument_policies import create_argument_policies_router
from app.api.v1.endpoints.admin.audit_chain import create_audit_chain_router
from app.api.v1.endpoints.admin.audit_export_destinations import create_audit_export_router
from app.api.v1.endpoints.admin.claude_desktop_export import (
    create_claude_desktop_export_router,
)
from app.api.v1.endpoints.admin.end_user_groups import create_end_user_groups_router
from app.api.v1.endpoints.admin.end_users import create_end_user_router
from app.api.v1.endpoints.admin.gateway_config import create_gateway_config_admin_router
from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
from app.api.v1.endpoints.admin.idp_servers import create_idp_admin_router
from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.api.v1.endpoints.admin.llm_providers import create_llm_providers_router
from app.api.v1.endpoints.admin.llm_routing_groups import create_llm_router_admin_router
from app.api.v1.endpoints.admin.mcp_catalog import create_mcp_catalog_router
from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
from app.api.v1.endpoints.admin.metrics import create_proxy_metrics_admin_router
from app.api.v1.endpoints.admin.oauth_app_clients import create_admin_oauth_app_clients_router
from app.api.v1.endpoints.admin.oauth_tokens import create_admin_oauth_tokens_router
from app.api.v1.endpoints.admin.proxy_logs import create_unified_proxy_logs_admin_router
from app.api.v1.endpoints.admin.resource_group_end_user_groups import (
    create_resource_group_end_user_groups_router,
)
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.endpoints.admin.server_configs import create_admin_server_configs_router
from app.api.v1.endpoints.admin.skill_sources import create_skill_sources_admin_router
from app.api.v1.endpoints.admin.system_alerts import create_system_alerts_router
from app.api.v1.endpoints.admin.system_logs_ws import (
    create_system_logs_ws_router,
    install_tail_handler,
)
from app.api.v1.endpoints.admin.system_ops import create_system_ops_router
from app.api.v1.endpoints.admin.system_servers import create_system_servers_admin_router
from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.api.v1.endpoints.admin.tool_approvals import create_tool_approvals_admin_router
from app.api.v1.endpoints.admin.video_generation_jobs import (
    create_video_generation_jobs_admin_router,
)
from app.api.v1.endpoints.admin.virtual_servers import create_virtual_server_router
from app.api.v1.endpoints.service_info import create_service_info_router
from app.api.v1.endpoints.auth.dcr import create_dcr_router
from app.api.v1.endpoints.public_assets import create_public_assets_router
from app.composition.features import ENTERPRISE_FEATURES
from app.composition.services import Services

V1 = "/v1"


@dataclass(frozen=True)
class RouterSpec:
    """One router the application mounts.

    `name` is unique and stable. `build` makes the router from the services.
    `feature` is the Enterprise feature the router belongs to, or `None` for
    the Community Edition.
    """

    name: str
    build: Callable[[Services], APIRouter]
    prefix: str = V1
    feature: Optional[str] = None


def mount_routers(app: FastAPI, services: Services, specs: Iterable[RouterSpec]) -> None:
    """Build and mount each router, in the order given."""
    for spec in specs:
        app.include_router(spec.build(services), prefix=spec.prefix)


# ---------------------------------------------------------------------------
# Build functions for routers that are imported when the application is
# created, or that need more than one statement.
# ---------------------------------------------------------------------------


def _compliance(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance import create_compliance_admin_router

    return create_compliance_admin_router(s.logger)







def _agent_cards(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.agent_cards import create_agent_cards_router

    return create_agent_cards_router(s.logger)


def _a2a_delegation_graphs(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.a2a_delegation_graphs import (
        create_a2a_delegation_graphs_router,
    )

    return create_a2a_delegation_graphs_router(s.logger)


def _a2a_tasks(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.a2a_tasks import create_a2a_tasks_router

    return create_a2a_tasks_router(s.logger)


def _ai_assets(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_assets import create_ai_assets_router

    return create_ai_assets_router(s.logger)


def _ai_systems(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router

    return create_ai_systems_router(s.logger)


def _ai_obligations(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_obligations import create_ai_obligations_router

    return create_ai_obligations_router(s.logger)


def _compliance_registry(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance_registry import (
        create_compliance_registry_router,
    )

    return create_compliance_registry_router(s.logger)


def _compliance_evaluation(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance_evaluation import (
        create_compliance_evaluation_router,
    )

    return create_compliance_evaluation_router(s.logger)


def _evidence(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.evidence import create_evidence_router

    return create_evidence_router(s.logger)


def _compliance_incidents(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance_incidents import (
        create_compliance_incidents_router,
    )

    return create_compliance_incidents_router(s.logger)


def _compliance_transparency(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance_transparency import (
        create_compliance_transparency_router,
    )

    return create_compliance_transparency_router(s.logger)


def _compliance_reports(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance_reports import (
        create_compliance_reports_router,
    )

    return create_compliance_reports_router(s.logger)


def _compliance_documentation(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.compliance_documentation import (
        create_compliance_documentation_router,
    )

    return create_compliance_documentation_router(s.logger)


def _assurance_check_reviews(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.assurance_check_reviews import (
        create_assurance_check_reviews_router,
    )

    return create_assurance_check_reviews_router(s.logger)


def _mission_control_cards(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.mission_control_cards import (
        create_mission_control_cards_router,
    )

    return create_mission_control_cards_router(s.logger)


def _ai_system_runs(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router

    return create_ai_system_runs_router(s.logger)


def _run_interventions(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.run_interventions import (
        create_run_interventions_router,
    )

    return create_run_interventions_router(s.logger)


def _ai_system_liveness(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_liveness import (
        create_ai_system_liveness_router,
    )

    return create_ai_system_liveness_router(s.logger)


def _ai_system_composition(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_composition import (
        create_ai_system_composition_router,
    )

    return create_ai_system_composition_router(s.logger)


def _data_erasure(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.data_erasure import create_data_erasure_router

    return create_data_erasure_router(s.logger)


def _ai_system_drift(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_drift import create_ai_system_drift_router

    return create_ai_system_drift_router(s.logger)


def _ai_system_incident_windows(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_incident_windows import (
        create_ai_system_incident_windows_router,
    )

    return create_ai_system_incident_windows_router(s.logger)


def _assurance_inbox(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.assurance_inbox import create_assurance_inbox_router

    return create_assurance_inbox_router(s.logger)


def _assurance_notifications(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.assurance_notifications import (
        create_assurance_notifications_router,
    )

    return create_assurance_notifications_router(s.logger, s.encryption_service)


def _assurance_checks(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.assurance_checks import create_assurance_checks_router

    return create_assurance_checks_router(s.logger)


def _assurance_digest(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.assurance_digest import create_assurance_digest_router

    return create_assurance_digest_router(s.logger)


def _ai_system_contracts(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_contracts import (
        create_ai_system_contracts_router,
    )

    return create_ai_system_contracts_router(s.logger)


def _ai_system_evidence(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_evidence import (
        create_ai_system_evidence_router,
    )

    return create_ai_system_evidence_router(s.logger)


def _standards(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.standards import create_standards_router

    return create_standards_router(s.logger)


def _ai_system_response(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_response import (
        create_ai_system_response_router,
    )

    return create_ai_system_response_router(s.logger)


def _ai_system_data_scope(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.ai_system_data_scope import (
        create_ai_system_data_scope_router,
    )

    return create_ai_system_data_scope_router(s.logger)


def _iso42001_aims(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.iso42001_aims import create_iso42001_aims_router

    return create_iso42001_aims_router(s.logger)




def _agent_identities(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.agent_identities import create_agent_identities_router

    return create_agent_identities_router(s.logger)


def _semantic_policies(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.semantic_policies import create_semantic_policies_router

    return create_semantic_policies_router(s.logger)


def _agent_registry(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.registry import create_agent_registry_router

    return create_agent_registry_router(s.logger)



def _mfa(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.mfa import create_mfa_router

    return create_mfa_router()


def _two_factor_login(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.mfa import create_2fa_login_router

    return create_2fa_login_router()


def _me(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.mfa import create_me_router

    return create_me_router()


def _system_logs_ws(s: Services) -> APIRouter:
    router = create_system_logs_ws_router(s.logger)
    # Install the ring-buffer logging handler so the tail starts capturing
    # log records before the first client connects. Idempotent, so safe to
    # call again on a hot reload.
    install_tail_handler()
    return router


def _saved_log_views(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.saved_log_views import create_saved_log_views_router

    return create_saved_log_views_router(logger=s.logger)


def _llm_catalog(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.llm_catalog import create_llm_catalog_router

    return create_llm_catalog_router(
        model_manager=s.llm_server_manager,
        logger=s.logger,
        encryption_service=s.encryption_service,
    )



def _llm_provider_catalog(s: Services) -> APIRouter:
    from app.api.v1.endpoints.admin.llm_provider_catalog import (
        create_llm_provider_catalog_router,
    )

    return create_llm_provider_catalog_router(logger=s.logger)


# ---------------------------------------------------------------------------
# The routers, in mount order.
# ---------------------------------------------------------------------------

ROUTERS: Tuple[RouterSpec, ...] = (
    RouterSpec("gateway_config", lambda s: create_gateway_config_admin_router(s.logger)),
    RouterSpec("system_servers", lambda s: create_system_servers_admin_router(s.logger)),
    # `/admin/tenants` is three routers. The
    # installation's own tenant, `/current*`: Community. Tenant management,
    # everything by tenant id and the list: Enterprise. Its `{tenant_id}`
    # never matches the word `current`, and the Knowledge Base routes share
    # no path with either, so the order of the three does not matter.
    RouterSpec("tenant_settings", lambda s: create_tenant_settings_router(s.logger)),
    RouterSpec("admin_users", lambda s: create_admin_users_router(s.logger)),
    RouterSpec(
        "guardrails",
        lambda s: create_guardrails_router(
            s.guardrails_service, s.logger, encryption_service=s.encryption_service
        ),
    ),
    RouterSpec("argument_policies", lambda s: create_argument_policies_router(s.logger)),
    # Compliance framework export endpoints.
    RouterSpec("compliance", _compliance),
    # A2A (Agent-to-Agent): agent card registry + signing. The router
    # enforces the `a2a` tenant feature flag internally; it is mounted
    # unconditionally so the flag is the only source of truth.
    RouterSpec("agent_cards", _agent_cards),
    # A2A: Mission Control delegation graphs over proxy_logs.
    RouterSpec("a2a_delegation_graphs", _a2a_delegation_graphs),
    # A2A v1.0 stateful task browser (read-only; the proxy writes).
    RouterSpec("a2a_tasks", _a2a_tasks),
    # AI Asset Registry (governed AI inventory).
    RouterSpec("ai_assets", _ai_assets),
    # AI System fleet board. A read-only projection over the
    # AI-System resource groups; the entity still lives in resource_groups.
    RouterSpec("ai_systems", _ai_systems),
    # Regulatory obligations: the dated catalog computed over the
    # AI Systems' declared facts. Never a compliance boolean.
    RouterSpec("ai_obligations", _ai_obligations),
    # The compliance framework registry (data, not code) with
    # per-tenant enablement, and the per-system compliance profile whose
    # EU AI Act block writes through to the group's classification columns.
    RouterSpec("compliance_registry", _compliance_registry),
    # Obligation evaluation: Overview, obligation detail
    # (statements with n-of-m, citations, catalog version, cross-framework
    # links), the Systems & Profiles table and the retention composition.
    RouterSpec("compliance_evaluation", _compliance_evaluation),
    # The Evidence Ledger. Records, tenant logs, tree
    # heads and receipts are written by the Rust core (it holds the signing
    # key and the append path); the CP reads them, serves RFC 6962 proofs
    # from the stored nodes, manages witnesses, and delegates record
    # verification to the core so an unreachable verifier is a finding.
    RouterSpec("evidence", _evidence),
    # Incidents, AI-interaction notice configs and the
    # transparency summary, Evidence Reports (+ bundles) and the Human
    # Oversight summary, documentation skeletons, blind judge reviews.
    RouterSpec("compliance_incidents", _compliance_incidents),
    RouterSpec("compliance_transparency", _compliance_transparency),
    RouterSpec("compliance_reports", _compliance_reports),
    RouterSpec("compliance_documentation", _compliance_documentation),
    RouterSpec("assurance_check_reviews", _assurance_check_reviews),
    # Mission Control card dashboard: the RG-card
    # landing page. Windows scale measurements; judgments stay pinned.
    RouterSpec("mission_control_cards", _mission_control_cards),
    # The run ledger. Read-only: runs are written by the
    # Rust core's finalizer so there is one definition of what a run is.
    RouterSpec("ai_system_runs", _ai_system_runs),
    # Operator abort of one in-flight run.
    RouterSpec("run_interventions", _run_interventions),
    # Liveness configuration + status. Detection itself
    # runs in the Rust core: it must fire on the ABSENCE of traffic, and
    # every periodic loop lives there with an operator-console beacon.
    RouterSpec("ai_system_liveness", _ai_system_liveness),
    # Composite AI Systems: dependency edges + the
    # Composition view (closure + edges + run segments + findings).
    RouterSpec("ai_system_composition", _ai_system_composition),
    # Data-subject erasure. Companion to the retention sweeper.
    RouterSpec("data_erasure", _data_erasure),
    # Baselines + drift findings. Read/triage only;
    # detection runs in the Rust core with the other sweepers.
    RouterSpec("ai_system_drift", _ai_system_drift),
    # Incident windows: declared periods excluded from judgement,
    # kept as evidence.
    RouterSpec("ai_system_incident_windows", _ai_system_incident_windows),
    # Assurance Inbox: the per-tenant triage queue of
    # findings awaiting human review; dispositions are report evidence.
    RouterSpec("assurance_inbox", _assurance_inbox),
    # Notification channels + real-time alert routing.
    # (Distinct from `admin_notifications`, the portal-inbox message sender.)
    RouterSpec("assurance_notifications", _assurance_notifications),
    # Continuous Assurance Checks: versioned spec
    # authoring, backtests, results. Deterministic specs are compile-checked
    # by the Rust core (the single CEL engine) before they can be stored.
    RouterSpec("assurance_checks", _assurance_checks),
    # Weekly digest: stored projections + live preview.
    RouterSpec("assurance_digest", _assurance_digest),
    # Hash-pinned contracts + the gated lifecycle. The
    # contract is generated from the resolved RG closure, never authored.
    RouterSpec("ai_system_contracts", _ai_system_contracts),
    # External assurance evidence: impact assessments, eval / red-team /
    # pen-test reports. Pointers with fingerprints, not a document store;
    # read by the lifecycle gate (high-risk FRIA floor) and the report.
    RouterSpec("ai_system_evidence", _ai_system_evidence),
    # The artifact standards (AI System Contract / Assurance Report):
    # the vendored JSON Schemas the exporters are pinned to, served so a
    # recipient can validate a document without ever having seen Brutor.
    RouterSpec("standards", _standards),
    # Response policies (the closed loop) + replay
    # visibility. The preview endpoint is required before arming: autonomy
    # resolves min-wins down the tree, so a policy on a parent restricts
    # every inheriting system below it.
    RouterSpec("ai_system_response", _ai_system_response),
    # Run data scope and cross-run record reach.
    RouterSpec("ai_system_data_scope", _ai_system_data_scope),
    # ISO/IEC 42001 AIMS: control coverage / SoA + evidence pack.
    RouterSpec("iso42001_aims", _iso42001_aims),
    # Agent Identity: agents as governable principals + per-agent grants.
    RouterSpec("agent_identities", _agent_identities),
    # Semantic Governance Policy: NL/LLM-judged policy.
    RouterSpec("semantic_policies", _semantic_policies),
    # Unified Agent Registry: governed + discovered coverage view.
    RouterSpec("agent_registry", _agent_registry),
    RouterSpec("end_users", lambda s: create_end_user_router(s.end_user_auth, s.logger)),
    # EndUserGroup admin endpoints (groups administrators create).
    RouterSpec("end_user_groups", lambda s: create_end_user_groups_router(s.logger)),
    # RG-side counterpart: lets the Resource Group ->
    # Members tab attach end-user groups so the operator doesn't have
    # to bounce to the User Groups page to wire the binding.
    RouterSpec(
        "resource_group_end_user_groups",
        lambda s: create_resource_group_end_user_groups_router(s.logger),
    ),
    # Public asset routes: user avatars at canonical
    # `/v1/assets/users/...` paths so the admin UI (which
    # hits control-plane directly in dev) can render the same
    # avatar_url values that the portal renders against the
    # proxy.
    RouterSpec("public_assets", lambda s: create_public_assets_router()),
    # 2FA / MFA + admin self-service routes.
    RouterSpec("mfa", _mfa),
    RouterSpec("two_factor_login", _two_factor_login),
    RouterSpec("me", _me),
    RouterSpec(
        "resource_groups",
        lambda s: create_resource_groups_router(s.usage_limit_enforcer, s.logger),
    ),
    # System alerts: downstream failures, health transitions, infra
    # threshold crossings, and internal errors (distinct from usage_alerts
    # which is quota-scoped). Populated by the Rust core proxy via the
    # services::system_alerts helper.
    RouterSpec("system_alerts", lambda s: create_system_alerts_router(s.logger)),
    # System Operations Console. Tenant-scoped operator
    # surface for runtime controls (overview, counters, caches,
    # audit, connections, tasks, sessions, logs, maintenance).
    # Distinct from `system_dashboard` above, which is
    # PLATFORM-scoped (system-admin only, cross-tenant rollups).
    RouterSpec("system_ops", lambda s: create_system_ops_router(s.logger)),
    # Audit hash-chain verification. Thin pass-through to the
    # Rust core, which owns the canonical row format; see the module docstring
    # for why the check cannot run here.
    RouterSpec("audit_chain", lambda s: create_audit_chain_router(s.logger)),
    # WebSocket log tail. Distinct router because
    # @router.websocket uses a different routing tree than @router.get/post.
    # The upgrade is authorised inside the handler by a single-use ticket
    # minted by an authenticated request of the same router (browsers can't
    # send Authorization on WS upgrades).
    RouterSpec("system_logs_ws", _system_logs_ws),
    RouterSpec(
        "service_info",
        lambda s: create_service_info_router(s.config_manager, s.logger),
        prefix="",
    ),
    RouterSpec("idps", lambda s: create_idp_admin_router(logger=s.logger)),
    # Audit-export destinations (scheduled S3 / webhook proxy_log
    # export). Needs the EncryptionService to encrypt secrets at rest.
    # The matching scheduler is started in the application lifespan:
    # endpoints are reachable as soon as the app comes up, the
    # scheduler joins a moment later.
    RouterSpec(
        "audit_export",
        lambda s: create_audit_export_router(
            encryption_service=s.encryption_service, logger=s.logger
        ),
    ),
    # RFC 7591 Inbound Dynamic Client Registration.
    # Public, unauthenticated endpoint at /v1/auth/dcr/.../register,
    # protected by per-tenant quota + audit log. See
    # `app/api/v1/endpoints/auth/dcr.py` for the security posture.
    RouterSpec(
        "dcr",
        lambda s: create_dcr_router(encryption_service=s.encryption_service, logger=s.logger),
    ),
    # Claude Desktop config export. Admin endpoint
    # that turns a user's resource-group MCP access into a
    # `claude_desktop_config.json` snippet they can paste into
    # ~/Library/Application Support/Claude/. See the docs ref guide
    # `claude_desktop_export_guide.md` for the operator workflow.
    RouterSpec(
        "claude_desktop_export",
        lambda s: create_claude_desktop_export_router(logger=s.logger),
    ),
    RouterSpec(
        "oauth_app_clients",
        lambda s: create_admin_oauth_app_clients_router(
            encryption_service=s.encryption_service,
            logger=s.logger,
            dcr_service=s.dcr_service,
        ),
    ),
    # Read + revoke view of user-held OAuth tokens (per-tenant,
    # per-MCP-server). Encrypted token material is never exposed; each
    # row is classified as active / expired_with_refresh /
    # expired_no_refresh / revoked / idp_unavailable so the admin UI
    # can answer "why did this MCP call 403?" without guessing.
    # `encryption_service` is needed to decrypt the access token when
    # best-effort calling the IdP's RFC 7009 revocation_endpoint on
    # revoke; the plaintext never leaves the request scope.
    RouterSpec(
        "oauth_tokens",
        lambda s: create_admin_oauth_tokens_router(
            logger=s.logger, encryption_service=s.encryption_service
        ),
    ),
    RouterSpec(
        "server_configs",
        lambda s: create_admin_server_configs_router(
            server_config_manager=s.server_config_manager, logger=s.logger
        ),
    ),
    RouterSpec(
        "mcp_servers",
        lambda s: create_mcp_server_router(
            s.mcp_server_manager, s.logger, encryption_service=s.encryption_service
        ),
    ),
    RouterSpec(
        "virtual_servers",
        lambda s: create_virtual_server_router(
            virtual_server_manager=s.virtual_server_manager, logger=s.logger
        ),
    ),
    RouterSpec(
        "mcp_catalog",
        lambda s: create_mcp_catalog_router(s.logger, encryption_service=s.encryption_service),
    ),
    RouterSpec("agent_skills", lambda s: create_agent_skills_admin_router(logger=s.logger)),
    RouterSpec(
        "agent_skill_workflows",
        lambda s: create_agent_skill_workflows_router(logger=s.logger),
    ),
    # Git-sourced skill repositories. The sync scheduler walks the
    # registered repos and POSTs discovered skills back via this same
    # router's /{id}/sync-ingest endpoint.
    RouterSpec("skill_sources", lambda s: create_skill_sources_admin_router(logger=s.logger)),
    # SKILL.md-portable export: download a skill as a ZIP that drops
    # cleanly into Claude Desktop / Cursor / Codex / VS Code / Kiro /
    # Goose, anywhere the Anthropic Agent Skills spec is honoured.
    RouterSpec(
        "agent_skills_export", lambda s: create_agent_skills_export_router(logger=s.logger)
    ),
    RouterSpec(
        "proxy_logs",
        lambda s: create_unified_proxy_logs_admin_router(
            logger=s.logger, log_service=s.proxy_logs_service
        ),
    ),
    # Saved log views: per-tenant named filter presets shared by the
    # proxy log + admin audit log search UIs. CRUD only; the live
    # /search endpoints already on the proxy_logs + admin_users routers
    # do the work of applying a view's stored envelope.
    RouterSpec("saved_log_views", _saved_log_views),
    RouterSpec(
        "metrics",
        lambda s: create_proxy_metrics_admin_router(s.proxy_metrics_service, s.logger),
    ),
    RouterSpec(
        "video_generation_jobs",
        lambda s: create_video_generation_jobs_admin_router(logger=s.logger),
    ),
    RouterSpec(
        "llm_models",
        lambda s: create_llm_models_router(
            model_manager=s.llm_server_manager,
            logger=s.logger,
            encryption_service=s.encryption_service,
        ),
    ),
    # Global LLM Catalog: browse the shared model catalog + import entries
    # into a tenant as configured llm_model instances.
    RouterSpec("llm_catalog", _llm_catalog),
    RouterSpec("llm_providers", lambda s: create_llm_providers_router(logger=s.logger)),
    # Global LLM Provider Catalog: browse the shared provider catalog the
    # admin UI dropdown reads when manually adding a model. Materialization
    # into per-tenant `llm_providers` happens on demand at model-create.
    RouterSpec("llm_provider_catalog", _llm_provider_catalog),
    RouterSpec(
        "llm_routing_groups",
        lambda s: create_llm_router_admin_router(
            router_service=s.llm_router_service, logger=s.logger
        ),
    ),
    RouterSpec("tool_approvals", lambda s: create_tool_approvals_admin_router(logger=s.logger)),
)
