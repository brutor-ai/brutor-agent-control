# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
WIRE CONTRACT pin — proxy_logs token values shared with the Rust core.

These proxy_type / proxy_subtype / agent_authz_decision strings are WRITTEN by
the Rust core (`brutor-gateway-core/src/constants.rs`) into proxy_logs and READ
here by string equality. There is no compile-time link between the two repos, so
a rename in one silently breaks the other.

This test pins the canonical values on the Python side; the Rust side is pinned
by `constants.rs::tests::wire_contract_*`. The hardcoded literals below ARE the
contract — changing a value here forces a deliberate edit, and must be matched
in the Rust pin (and vice-versa). Keep the two pin tests in lockstep.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.constants.proxy import (
    AgentAuthzDecision,
    GuardrailSurface,
    ProxySubtype,
    ProxyType,
)


def test_proxy_type_values_pinned():
    assert ProxyType.MCP.value == "mcp"
    assert ProxyType.VIRTUAL_MCP.value == "virtual-mcp"  # hyphen — matches Rust
    assert ProxyType.LLM.value == "llm"
    assert ProxyType.SKILL.value == "skill"
    assert ProxyType.OAUTH.value == "oauth"
    assert ProxyType.A2A.value == "a2a"


def test_agent_authz_decision_values_pinned():
    assert AgentAuthzDecision.ALLOW.value == "allow"
    assert AgentAuthzDecision.DENY.value == "deny"
    assert AgentAuthzDecision.APPROVAL_REQUIRED.value == "approval_required"


def test_proxy_subtype_values_pinned():
    # Mirror of brutor-gateway-core/src/constants.rs::proxy_subtype (the subset
    # the control plane reads). Must equal the Rust pin's values exactly.
    assert ProxySubtype.AGENT_AUTHZ_BLOCK.value == "agent_authz_block"
    assert ProxySubtype.AGENT_AUTHZ_SHADOW.value == "agent_authz_shadow"
    assert ProxySubtype.SEMANTIC_POLICY_BLOCK.value == "semantic_policy_block"
    assert ProxySubtype.SEMANTIC_POLICY_SHADOW.value == "semantic_policy_shadow"
    assert ProxySubtype.APPROVAL_REQUIRED.value == "approval_required"
    # An operator stopping ONE run, kept separable from
    # AUTONOMY_BLOCK (the whole agent or AI System) in the audit trail.
    assert ProxySubtype.RUN_ABORTED.value == "run_aborted"
    assert ProxySubtype.RESIDENCY_VIOLATION.value == "residency_violation"
    assert ProxySubtype.AUTONOMY_BLOCK.value == "autonomy_block"
    # Refusal subtypes sealed as `denied` / `engine_failure`.
    assert ProxySubtype.RUN_CAP_REACHED.value == "run_cap_reached"
    assert ProxySubtype.ACCESS_DENIED.value == "access_denied"
    assert ProxySubtype.SKILL_QUOTA_EXCEEDED.value == "skill_quota_exceeded"
    assert ProxySubtype.SKILL_CONCURRENCY_LIMIT.value == "skill_concurrency_limit"
    assert ProxySubtype.APPROVAL_TOKEN_INVALID.value == "approval_token_invalid"
    assert ProxySubtype.ENGINE_FAILURE.value == "engine_failure"
    # Accepted-caller enforcement refused the A2A call
    # before the upstream was contacted.
    assert ProxySubtype.UNACCEPTED_CALLER.value == "unaccepted_caller"
    # An LLM-surface governance policy refused the request pre-provider.
    assert ProxySubtype.LLM_GOVERNANCE_BLOCK.value == "llm_governance_block"


def test_chain_integrity_values_pinned():
    """XREPO-4 — `ai_system_runs.chain_integrity` is written by the Rust
    core (`run_ledger.rs::chain`) and read by name in tier-A CEL checks.

    The literals below ARE the contract, mirroring `run_ledger.rs:94-110`
    and the SQL pin at `:135`
    (`chain_integrity IN ('intact', 'client_asserted', 'observed_external')`).
    """
    from app.database.models.ai_system_run import CHAIN_INTEGRITIES

    assert CHAIN_INTEGRITIES == frozenset({
        "intact",
        "client_asserted",
        "observed_external",
        "partial",
        "broken",
    })


def test_run_fact_guide_chain_integrity_matches_the_column_vocabulary():
    """The guide is what an author reads when writing a check.

    It used to restate the values by hand and had drifted to `dropped` /
    `sig_failed` — both `proxy_logs.trace_continuity` values, never
    written to this column. A check authored against one of those
    compiles fine and then evaluates false on every run forever, which
    reads as "nothing flagged". Nothing else can catch that, so the guide
    is pinned against the column's own vocabulary here.
    """
    import re

    from app.constants.assurance import RUN_FACT_GUIDE
    from app.database.models.ai_system_run import CHAIN_INTEGRITIES

    documented = set(re.findall(r"'([a-z_]+)'", RUN_FACT_GUIDE["chain_integrity"]))
    assert documented == set(CHAIN_INTEGRITIES), (
        "RUN_FACT_GUIDE['chain_integrity'] disagrees with CHAIN_INTEGRITIES: "
        f"guide-only={sorted(documented - set(CHAIN_INTEGRITIES))}, "
        f"column-only={sorted(set(CHAIN_INTEGRITIES) - documented)}"
    )


def test_guardrail_surface_values_pinned():
    """Guardrail surfaces are a SILENT-failure vocabulary.

    The Rust proxy's per-surface gate returns "not enabled" for a token it
    does not recognise, so a drift between these values and
    `constants.rs::guardrail_surface` does not raise — it turns the checks on
    that surface off. Pinned literally on both sides so the drift is a test
    failure instead.
    """
    assert GuardrailSurface.CHAT_INPUT.value == "chat_input"
    assert GuardrailSurface.CHAT_OUTPUT.value == "chat_output"
    assert GuardrailSurface.EMBEDDINGS_INPUT.value == "embeddings_input"
    assert GuardrailSurface.MODERATION_INPUT.value == "moderation_input"
    assert GuardrailSurface.MCP.value == "mcp"
    assert GuardrailSurface.SKILL.value == "skill"
    assert GuardrailSurface.A2A_INBOUND.value == "a2a_inbound"
    assert GuardrailSurface.A2A_OUTBOUND.value == "a2a_outbound"


def test_declared_outcome_wire_contract_pinned():
    """Declared outcomes — the header names, the id
    grammar and the three vocabularies the Rust core WRITES at finalize
    (`outcome_conformance`, `error_cause`, the run class) and the control
    plane reads. Mirrored in the core and pinned there; the literals below
    ARE the contract. `client_outcome` / `X-Brutor-Run-Outcome` are gone."""
    import re

    from app.constants import outcomes as o
    from app.database.models import ai_system_run

    assert o.OUTCOME_ID_HEADER == "X-Brutor-Outcome-Id"
    assert o.OUTCOME_CONFORMANCE_HEADER == "X-Brutor-Outcome-Conformance"
    assert o.OUTCOME_ID_BODY_FIELD == "outcome_id"
    assert o.TASK_METADATA_OUTCOME_ID == "brutor.outcome.id"
    assert o.TASK_METADATA_OUTCOME_CONFORMANCE == "brutor.outcome.conformance"
    assert o.OTEL_OUTCOME_ID_ATTR == "brutor.outcome.id"
    assert o.OUTCOME_ID_PATTERN == r"^[a-z0-9_-]{1,48}(\.[a-z0-9_-]{1,48})?$"
    assert o.OUTCOME_ID_MAX_STORED == 128
    for ok in ("no_applications_available", "application_processed.loan_approved",
               "a", "a-b_c.d-e_f", "x" * 48 + "." + "y" * 48):
        assert re.match(o.OUTCOME_ID_PATTERN, ok), ok
    for bad in ("", "A", "a.b.c", "a.", ".b", "a b", "x" * 49, "a." + "y" * 49, "a/b"):
        assert not re.match(o.OUTCOME_ID_PATTERN, bad), bad

    assert o.OUTCOME_CONFORMANCES == ("declared", "undeclared", "unreported")
    assert o.ERROR_CAUSES == (
        "blocked_by_policy", "rate_limited", "model_unavailable", "model_rejected",
        "tool_failed", "skill_failed", "agent_unreachable", "delegate_deviated",
        "iteration_limit", "client_reported",
    )
    assert set(o.ERROR_CAUSE_LABELS) == set(o.ERROR_CAUSES)
    assert o.RUN_CLASSES == ("success", "no_work", "deviation", "failed", "not_decisive")
    assert o.EXPECT_KEYS == (
        "max_actions", "max_model_calls", "max_cost_usd",
        "requires_capabilities", "forbids_capabilities",
    )
    assert (o.MAX_OUTCOMES, o.MAX_VARIANTS, o.LABEL_MAX) == (32, 32, 128)
    assert [x["id"] for x in o.starter_catalogue()] == ["nothing_to_do", "task_completed"]

    # The CEL run-facts context: a key was removed, so the version moved (core: 2).
    from app.constants.assurance import RUN_FACT_KEYS, RUN_FACTS_VERSION

    assert RUN_FACTS_VERSION == 2
    assert {"outcome_id", "outcome_conformance", "error_cause", "run_class"} <= set(RUN_FACT_KEYS)
    assert "client_outcome" not in RUN_FACT_KEYS

    # Removed, not aliased.
    assert not hasattr(ai_system_run, "CLIENT_OUTCOMES")
    assert not hasattr(ai_system_run, "TERMINAL_BLOCKED_POLICY")
    assert "client_outcome" not in ai_system_run.AISystemRun.__table__.columns


def test_terminal_states_pinned():
    """`blocked_policy` is not a terminal state any more (a
    gateway refusal is `errored` with cause blocked_by_policy), neither on
    the run nor as an `X-Brutor-Run-End` value."""
    from app.constants.proxy import RunTerminalState
    from app.database.models.ai_system_run import TERMINAL_STATES

    expected = {"completed", "completed_degraded", "exhausted", "errored", "abandoned", "cancelled"}
    assert TERMINAL_STATES == expected
    assert {s.value for s in RunTerminalState} == expected


def test_run_source_values_pinned():
    """CP-18 — `ai_system_runs.source`, mirroring `run_ledger.rs::source`.

    This is the column that separates traffic the gateway *witnessed* from
    traffic an OTel exporter merely *reported*, so a drift here does not
    just break a check: it lets a report present ingested telemetry as
    gateway-controlled evidence."""
    from app.database.models.ai_system_run import RUN_SOURCES

    assert RUN_SOURCES == frozenset({"gateway", "otel", "mixed"})


def test_proxy_log_source_values_pinned():
    """CP-18 — `proxy_logs.source` is stamped per step by the core (the
    proxy path) and by the OTel inlet (the ingest path). The run-level
    `source` above is derived from these, so `mixed` is a run value only —
    a step is one or the other."""
    from app.database.models.proxy_log import ProxyLog

    column = ProxyLog.__table__.columns["source"]
    assert column.server_default.arg == "gateway"
    documented = set(part.strip().split(" ")[0] for part in column.comment.split("|"))
    assert documented == {"gateway", "otel"}, column.comment


def test_run_fact_guide_matches_the_run_vocabularies():
    """The guide is what the NL assist and the check-authoring UI show an
    author. It restated these value lists by hand, which is exactly how the
    `chain_integrity` row came to name two values that are never written."""
    import re

    from app.constants.assurance import RUN_FACT_GUIDE
    from app.constants.outcomes import ERROR_CAUSES, OUTCOME_CONFORMANCES, RUN_CLASSES
    from app.database.models.ai_system_run import RUN_SOURCES, TERMINAL_STATES

    # The declared-outcome facts replace `client_outcome`.
    assert "client_outcome" not in RUN_FACT_GUIDE
    for key, vocab in (
        ("outcome_conformance", OUTCOME_CONFORMANCES),
        ("run_class", RUN_CLASSES),
        ("error_cause", ERROR_CAUSES),
        ("terminal_state", TERMINAL_STATES),
    ):
        named = set(re.findall(r"'([a-z_]+)'", RUN_FACT_GUIDE[key]))
        assert named == set(vocab), (key, sorted(named ^ set(vocab)))

    sources = set(re.findall(r"'([a-z_]+)'", RUN_FACT_GUIDE["source"]))
    assert sources == set(RUN_SOURCES), (
        "RUN_FACT_GUIDE['source'] disagrees with RUN_SOURCES: "
        f"guide-only={sorted(sources - set(RUN_SOURCES))}, "
        f"column-only={sorted(set(RUN_SOURCES) - sources)}"
    )


# ---------------------------------------------------------------------------
# The semantic-policy vocabularies are declared, not just enforced
# ---------------------------------------------------------------------------


def test_semantic_policy_response_literals_match_the_database_vocabulary():
    """The API schema must carry the constraint the database enforces.

    `SemanticPolicy` validates `action` and `mode` against `POLICY_ACTIONS` /
    `POLICY_MODES` on write, so the values were always constrained in fact.
    The response models declared plain `str`, which discarded that at the API
    boundary: the OpenAPI schema said "string", the generated TypeScript said
    `string`, and the Admin Console re-declared the real union by hand.

    A hand-written narrowing is an assertion nothing verifies. Widen the
    server set without touching the console and it keeps compiling while
    silently mis-handling the new value — so the two are pinned together here
    rather than left to agree by coincidence.
    """
    import typing

    from app.api.v1.models.admin.semantic_policy import PolicyAction, PolicyMode
    from app.database.models.semantic_policy import POLICY_ACTIONS, POLICY_MODES

    assert set(typing.get_args(PolicyAction)) == set(POLICY_ACTIONS), (
        "SemanticPolicyResponse.action no longer matches POLICY_ACTIONS. Update "
        "both, and regenerate the OpenAPI snapshot and the admin console's "
        "generated types in the same change."
    )
    assert set(typing.get_args(PolicyMode)) == set(POLICY_MODES), (
        "SemanticPolicyResponse.mode no longer matches POLICY_MODES."
    )


def test_agent_identity_response_literals_match_the_database_vocabulary():
    """Same shape as the semantic-policy check, third occurrence.

    `AgentIdentityResponse` typed `source`, `enforcement_mode` and `status` as
    `str` while documenting the real unions in COMMENTS
    (`# enforce | dry_run`). A comment is not a contract: the schema said
    "string", the generated TypeScript said `string`, and the console
    re-declared the unions by hand.

    Enough repetitions to state the rule: if a validator constrains a column,
    the response model should say so. Otherwise the constraint stops at the
    database and every consumer re-invents it without verification.
    """
    import typing

    from app.api.v1.models.admin.agent_identity import (
        AgentEnforcementMode,
        AgentSource,
        AgentStatus,
    )
    from app.database.models.agent_identity import (
        AGENT_SOURCES,
        AGENT_STATUSES,
        ENFORCEMENT_MODES,
    )

    assert set(typing.get_args(AgentSource)) == set(AGENT_SOURCES)
    assert set(typing.get_args(AgentEnforcementMode)) == set(ENFORCEMENT_MODES)
    assert set(typing.get_args(AgentStatus)) == set(AGENT_STATUSES)


def test_tool_approval_status_values_pinned():
    # Mirror of brutor-gateway-core/src/routes/portal_approvals.rs::approval_status.
    # Both planes write tool_approval_requests.status; the core pins the same
    # literals and guards every status it writes (approval_status tests).
    from app.constants import approvals

    assert approvals.ALL_STATUSES == ("pending", "approved", "consumed", "rejected", "expired")
    assert approvals.APPROVED_STATUSES == ("approved", "consumed")
    assert approvals.FILTERS == ("pending", "approved", "consumed", "rejected", "expired", "all")


def test_run_segment_outcome_values_pinned():
    """`ai_system_run_segments.outcome` is WRITTEN by the Rust run
    ledger and READ here (liveness of on-demand services, dependency health,
    the Composition tab). The literals ARE the contract; the table's CHECK
    constraint (`a203_declared_outcomes`) and the Rust writer must carry the
    same four. There is no `blocked`: a refused delegation is `failed` with segment
    error_cause blocked_by_policy."""
    from app.database.models.ai_system_run_segment import (
        SEGMENT_OUTCOMES,
        SEGMENT_UNANSWERED_OUTCOMES,
        AISystemRunSegment,
    )

    assert SEGMENT_OUTCOMES == ("answered", "failed", "rejected", "timed_out")
    check = next(c for c in AISystemRunSegment.__table__.constraints
                 if getattr(c, "name", "") == "ck_ai_system_run_segments_outcome")
    assert "'blocked'" not in str(check.sqltext)
    # Only availability failures make a service silent; a refusal (the
    # delegate declined) is an answer.
    assert SEGMENT_UNANSWERED_OUTCOMES == frozenset({"failed", "timed_out"})


def test_dependency_metric_prefix_pinned():
    """`dependency:<target>` drift events are WRITTEN here
    (the dependency-health emitter, on a ROOT) and the Rust drift sweeper
    excludes that namespace from its own per-class cooldown
    (`drift/mod.rs::metric::CP_DEPENDENCY_PREFIX`, pinned by
    `profiles.rs::the_core_never_emits_a_key_in_the_control_planes_namespace`).
    A rename on one side would let a CP finding silence the core's own alert."""
    from app.services.ai_system_service_signals import DEPENDENCY_METRIC_PREFIX

    assert DEPENDENCY_METRIC_PREFIX == "dependency:"


def test_composition_vocabulary_matches_the_response_literals():
    """Composite AI Systems — the Admin UI switches on these strings. The service
    constants and the Pydantic Literals must be the same sets, or a value
    the service emits fails response validation (500) or a Literal accepts
    one the service never produces."""
    import typing

    from app.api.v1.models.admin import ai_system_composition as m
    from app.api.v1.models.admin.ai_system import AISystemLiveness, AISystemSummary
    from app.database.models.ai_system_dependency import (
        DEPENDENCY_CRITICALITIES,
        DEPENDENCY_ROLES,
    )
    from app.services import ai_system_composition as svc
    from app.services.ai_system_liveness_state import LIVENESS_STATES

    assert set(typing.get_args(m.DependencyRole)) == set(DEPENDENCY_ROLES) == {
        "shared_service", "external",
    }
    assert set(typing.get_args(m.DependencyCriticality)) == set(DEPENDENCY_CRITICALITIES) == {
        "critical", "degradable",
    }
    assert set(typing.get_args(m.DependencyHealth)) == set(svc.DEPENDENCY_HEALTHS) == {
        "healthy", "degraded", "idle", "unknown", "assumed",
    }
    assert set(typing.get_args(m.SystemRole)) == set(svc.SYSTEM_ROLES) == {
        "root", "shared_service", "root_and_service",
    }
    assert set(svc.ROLE_FILTERS) == {"root", "shared_service", "external_deps"}
    assert svc.COMPOSITION_FINDING_CODES == (
        "composition.undeclared_dependency",
        "composition.unaccepted_caller",
        "composition.declared_never_observed",
        "composition.role_mismatch",
        "composition.dependency_cycle",
        # Grants bound to declarations, and pins
        "composition.grant_without_edge",
        "composition.edge_without_grant",
        "composition.dependency_pin_stale",
    )
    role_field = AISystemSummary.model_fields["role"].annotation
    assert set(typing.get_args(role_field)) == set(svc.SYSTEM_ROLES)
    state_field = AISystemLiveness.model_fields["state"].annotation
    assert set(typing.get_args(state_field)) == set(LIVENESS_STATES) == {
        "alive", "silent", "idle", "unstarted", "not_configured",
    }


def test_composite_compliance_vocabulary_pinned():
    """Composite compliance — values the Admin UI switches on and stores back.

    `supplier_information` is an evidence kind (the written agreement a
    system relies on from a dependency's supplier, EU AI Act Art 25(4)); the
    assumption evidence kinds are the enum the strict assumptions schema
    accepts. The service constant and the Pydantic Literal must be the same
    set, or the schema refuses a value the UI offers (or accepts one nobody
    reads)."""
    import typing

    from app.database.models.ai_system_evidence import (
        EDGE_EVIDENCE_KINDS,
        EVIDENCE_KINDS,
        EVIDENCE_SUPPLIER_INFORMATION,
    )
    from app.services.ai_system_composition import (
        ASSUMPTION_EVIDENCE_KINDS,
        DependencyAssumptions,
    )
    from app.services.compliance.sealing_hooks import STATE_SEPARABILITY

    assert EVIDENCE_SUPPLIER_INFORMATION == "supplier_information"
    assert EVIDENCE_KINDS == {
        "impact_assessment", "eval_report", "red_team_report", "pen_test_report",
        "risk_assessment", "data_governance", "dpia", "supplier_information", "other",
    }
    assert EDGE_EVIDENCE_KINDS == {"supplier_information"}
    assert ASSUMPTION_EVIDENCE_KINDS == (
        "signed_agent_card", "attestation", "certification", "contract", "other",
    )
    field = DependencyAssumptions.model_fields["evidence_provided"].annotation
    (inner_list,) = [a for a in typing.get_args(field) if a is not type(None)]
    (literal,) = typing.get_args(inner_list)
    assert set(typing.get_args(literal)) == set(ASSUMPTION_EVIDENCE_KINDS)
    assert set(DependencyAssumptions.model_fields) == {
        "data_classes_shared", "retention_at_delegate", "human_oversight_at_delegate",
        "expected_availability_pct", "expected_p95_latency_ms", "evidence_provided",
    }
    assert STATE_SEPARABILITY == "separability"


def test_agent_implementation_wire_contract_pinned():
    """Agent implementation identity. Every value here is either
    what an agent must SEND (headers, baggage keys, token claims, the MCP
    `_meta` key) or what the core WRITES and the control plane READS
    (`proxy_logs.agent_impl_source`, `agent_impl_trust` / release trust, the
    drift cause rung). Mirrored in `brutor-gateway-core/src/constants.rs`
    and pinned there; the literals below ARE the contract."""
    from app.constants.agent_implementation import (
        AGENT_IMPL_SOURCES,
        AGENT_IMPL_TRUST_LEVELS,
        DRIFT_CAUSE_AGENT_RELEASE,
        LIFECYCLE_AGENT_NEW_RELEASE,
        MCP_CLIENT_INFO_META_KEY,
        NO_BUILD,
        RELEASE_FINGERPRINT_CAP,
        AgentImplBaggageKey,
        AgentImplHeader,
        AgentImplSource,
        AgentImplTokenClaim,
        AgentImplTrust,
    )

    assert {h.name: h.value for h in AgentImplHeader} == {
        "NAME": "X-Brutor-Agent-Name",
        "VERSION": "X-Brutor-Agent-Version",
        "BUILD": "X-Brutor-Agent-Build",
        "CLIENT": "X-Brutor-Client",
    }
    assert {k.name: k.value for k in AgentImplBaggageKey} == {
        "NAME": "gen_ai.agent.name",
        "VERSION": "gen_ai.agent.version",
        "BUILD": "gen_ai.agent.build",
    }
    assert {c.name: c.value for c in AgentImplTokenClaim} == {
        "NAME": "software_id",
        "VERSION": "software_version",
        "BUILD": "software_build",
    }
    assert MCP_CLIENT_INFO_META_KEY == "io.modelcontextprotocol/clientInfo"
    # Precedence order IS the enum order: verified token first.
    assert [s.value for s in AgentImplSource] == [
        "token", "mcp_client_info", "baggage", "header",
    ]
    assert AGENT_IMPL_SOURCES == {"token", "mcp_client_info", "baggage", "header"}
    assert [t.value for t in AgentImplTrust] == ["verified", "declared"]
    assert AGENT_IMPL_TRUST_LEVELS == {"verified", "declared"}
    assert DRIFT_CAUSE_AGENT_RELEASE == "agent_release"
    assert LIFECYCLE_AGENT_NEW_RELEASE == "agent.new_release"
    assert NO_BUILD == ""
    assert RELEASE_FINGERPRINT_CAP == 16


def test_agent_implementation_vocabulary_matches_the_schema_and_ladder():
    """The same vocabularies, where they are ENFORCED: the CHECK constraints
    the core's writes must satisfy (`a187_agent_implementation`, identical in the ORM for fresh
    installs), the cause ladder rung order (mirror of the core's
    `drift/cause.rs`: after contract_change, before model_change), the
    lifecycle event type, the finding codes and the inbox category."""
    import re

    from app.constants.agent_implementation import (
        AGENT_IMPL_SOURCES,
        AGENT_IMPL_TRUST_LEVELS,
        DRIFT_CAUSE_AGENT_RELEASE,
        LIFECYCLE_AGENT_NEW_RELEASE,
    )
    from app.database.models.agent_implementation_release import AgentImplementationRelease
    from app.database.models.ai_system_drift import CAUSE_LADDER, DRIFT_AGENT, DRIFT_CLASSES
    from app.database.models.ai_system_lifecycle_event import (
        EVENT_AGENT_NEW_RELEASE,
        LIFECYCLE_EVENT_TYPES,
    )
    from app.database.models.ai_system_run import AISystemRun
    from app.database.models.ai_system_run_segment import AISystemRunSegment
    from app.database.models.assurance_inbox import INBOX_CATEGORIES
    from app.database.models.proxy_log import ProxyLog
    from app.services.agent_implementation import BUILD_PATTERN, NAME_PATTERN, VERSION_PATTERN
    from app.services.assurance_checks.agent import AGENT_FINDING_CODES, INBOX_CATEGORY_AGENT
    from app.services.ai_system_service_signals import AGENT_FINDING_PREFIX

    def check_values(model, name):
        (ck,) = [c for c in model.__table__.constraints if getattr(c, "name", None) == name]
        return set(re.findall(r"'([^']+)'", str(ck.sqltext)))

    assert check_values(ProxyLog, "ck_proxy_logs_agent_impl_source") == AGENT_IMPL_SOURCES
    assert check_values(AISystemRun, "ck_ai_system_runs_agent_impl_trust") == AGENT_IMPL_TRUST_LEVELS
    assert check_values(
        AISystemRunSegment, "ck_ai_system_run_segments_agent_impl_trust") == AGENT_IMPL_TRUST_LEVELS
    assert check_values(
        AgentImplementationRelease, "ck_agent_implementation_releases_trust") == AGENT_IMPL_TRUST_LEVELS

    i = CAUSE_LADDER.index(DRIFT_CAUSE_AGENT_RELEASE)
    assert CAUSE_LADDER[i - 1] == "contract_change" and CAUSE_LADDER[i + 1] == "model_change"
    assert DRIFT_AGENT == "agent" and DRIFT_AGENT in DRIFT_CLASSES
    assert EVENT_AGENT_NEW_RELEASE == LIFECYCLE_AGENT_NEW_RELEASE
    assert LIFECYCLE_EVENT_TYPES == ("stage_transition", "agent.new_release")

    assert AGENT_FINDING_CODES == (
        "agent.unapproved_release",
        "agent.name_mismatch",
        "agent.release_missing",
        "agent.undeclared_change",
    )
    assert all(c.startswith(AGENT_FINDING_PREFIX) for c in AGENT_FINDING_CODES)
    assert INBOX_CATEGORY_AGENT == "agent" and INBOX_CATEGORIES[-1] == "agent"

    # Bounds — the core normalises to exactly these.
    assert NAME_PATTERN.pattern == r"^[a-z0-9._-]{1,64}$"
    assert VERSION_PATTERN.pattern == r"^[A-Za-z0-9.+_-]{1,64}$"
    assert BUILD_PATTERN.pattern == r"^[A-Za-z0-9:._-]{1,128}$"


def test_drift_resolution_wire_contract_pinned():
    """Drift resolution — the control plane WRITES resolution kinds, baseline epochs
    and incident windows; the Rust core READS the epochs and windows in its
    baseline and drift sweeps and applies THE SAME exclusion rule to the
    samples it grades. Mirrored in `brutor-gateway-core/src/constants.rs`
    and pinned there; the literals below ARE the contract.

    The rule (both planes): a sample is excluded when its timestamp lies in
    [starts_at, ends_at) of an incident window whose ai_system_id is NULL
    (tenant-wide) or one of the sample's systems; or, for a keyed metric,
    before the latest epoch of (system, metric, basis) whose caller is the
    baseline's caller key or 'all' (a per-caller baseline honours its
    caller's epochs and the aggregate's; the aggregate honours 'all' only). Runs: started_at / ai_system_id. Segments:
    first_action_at / segment_system_id + root_system_id."""
    from datetime import datetime, timezone

    from app.api.v1.models.admin import drift_resolution as m
    from app.constants.drift_resolution import (
        ENVELOPE_METRIC_PREFIX,
        EPOCH_ALL_CALLERS,
        EPOCH_KINDS,
        HUMAN_RESOLUTION_KINDS,
        INCIDENT_CAUSE_CATEGORIES,
        INCIDENT_RESOURCE_TYPES,
        RESOLUTION_KINDS,
        TABLE_BASELINE_EPOCHS,
        TABLE_INCIDENT_WINDOWS,
    )
    from app.services.ai_system_judgement_exclusion import (
        FACT_DRIFT_CLASSES,
        PURPOSE_RATE,
        Span,
        judged_sql,
        sample_excluded,
    )

    # Windows excuse PERFORMANCE, never FACTS.
    assert PURPOSE_RATE == "rate"
    assert FACT_DRIFT_CLASSES == {"agent", "conformance"}

    import typing

    assert RESOLUTION_KINDS == ("accepted_change", "cause_fixed", "false_positive", "auto_cleared")
    assert HUMAN_RESOLUTION_KINDS == ("accepted_change", "cause_fixed", "false_positive")
    assert EPOCH_KINDS == (
        "accepted_change", "false_positive", "measurement_change", "catalogue_change",
    )
    # An epoch names a bucket; 'all' covers every bucket of the metric.
    from app.constants.drift_resolution import EPOCH_ALL_BUCKETS

    assert EPOCH_ALL_BUCKETS == "all"
    assert INCIDENT_CAUSE_CATEGORIES == (
        "provider_outage", "platform_incident", "maintenance", "other",
    )
    assert INCIDENT_RESOURCE_TYPES == (
        "model", "provider", "mcp_server", "skill", "knowledge_base", "agent",
    )
    assert EPOCH_ALL_CALLERS == "all"
    assert ENVELOPE_METRIC_PREFIX == "envelope:"
    assert TABLE_BASELINE_EPOCHS == "ai_system_baseline_epochs"
    assert TABLE_INCIDENT_WINDOWS == "ai_system_incident_windows"
    # The core's cause rung 0 (`drift/cause.rs`): a change point inside (or
    # within CAUSE_WINDOW_HOURS of) a declared incident window.
    from app.database.models.ai_system_drift import (
        CAUSE_INCIDENT_WINDOW,
        CAUSE_LADDER,
        CAUSE_WINDOW_HOURS,
        CAUSES,
        AISystemDriftEvent,
    )

    assert CAUSE_INCIDENT_WINDOW == "incident_window"
    assert CAUSE_LADDER[0] == CAUSE_INCIDENT_WINDOW and CAUSE_LADDER[1] == "contract_change"
    assert CAUSE_INCIDENT_WINDOW in CAUSES and CAUSE_WINDOW_HOURS == 2
    assert AISystemDriftEvent(attributed_cause="incident_window").attributed_cause == "incident_window"
    # The API Literals are the same sets (the Admin UI switches on them).
    assert typing.get_args(m.ResolutionKind) == RESOLUTION_KINDS
    assert typing.get_args(m.HumanResolutionKind) == HUMAN_RESOLUTION_KINDS
    assert typing.get_args(m.EpochKind) == EPOCH_KINDS
    assert typing.get_args(m.IncidentCauseCategory) == INCIDENT_CAUSE_CATEGORIES
    assert typing.get_args(m.IncidentResourceType) == INCIDENT_RESOURCE_TYPES

    # The window interval is half-open; the epoch floor keeps samples AT it.
    t = lambda h: datetime(2026, 9, 27, h, tzinfo=timezone.utc)  # noqa: E731
    w = [Span(t(21), t(23))]
    assert sample_excluded(t(21), windows=w) is True
    assert sample_excluded(t(22), windows=w) is True
    assert sample_excluded(t(23), windows=w) is False
    assert sample_excluded(t(20), windows=w) is False
    assert sample_excluded(t(5), floor=t(6)) is True
    assert sample_excluded(t(6), floor=t(6)) is False
    # The SQL the core mirrors (its fragment is pinned against the same shape).
    assert judged_sql("r.tenant_id", "r.started_at", "r.ai_system_id", purpose="rate") == (
        "NOT EXISTS (SELECT 1 FROM ai_system_incident_windows iw "
        "WHERE iw.tenant_id = r.tenant_id AND (iw.ai_system_id IS NULL OR "
        "iw.ai_system_id = r.ai_system_id) "
        "AND r.started_at >= iw.starts_at AND r.started_at < iw.ends_at)"
    )
