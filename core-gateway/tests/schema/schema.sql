--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: approvalstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.approvalstatus AS ENUM (
    'NOT_REQUIRED',
    'PENDING',
    'APPROVED',
    'REJECTED',
    'EXPIRED'
);


--
-- Name: assettype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.assettype AS ENUM (
    'TEMPLATE',
    'IMAGE',
    'DATA'
);


--
-- Name: dependencytype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dependencytype AS ENUM (
    'REQUIRED',
    'OPTIONAL',
    'CONDITIONAL'
);


--
-- Name: executionstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.executionstatus AS ENUM (
    'PENDING',
    'RUNNING',
    'COMPLETED',
    'FAILED',
    'CANCELLED',
    'TIMED_OUT'
);


--
-- Name: grouptype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.grouptype AS ENUM (
    'ORGANIZATION',
    'DIVISION',
    'DEPARTMENT',
    'BUSINESS_UNIT',
    'TEAM',
    'PROJECT',
    'PROGRAM',
    'INITIATIVE',
    'AI_SYSTEM',
    'CUSTOM'
);


--
-- Name: policybundlestatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.policybundlestatus AS ENUM (
    'DRAFT',
    'VALIDATED',
    'ACTIVE',
    'SUPERSEDED',
    'ROLLED_BACK'
);


--
-- Name: policyeventtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.policyeventtype AS ENUM (
    'VALIDATED',
    'APPLIED',
    'ROLLED_BACK',
    'DRY_RUN',
    'SUPERSEDED'
);


--
-- Name: scriptexecutionmode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.scriptexecutionmode AS ENUM (
    'SANDBOX',
    'TRUSTED',
    'DISABLED'
);


--
-- Name: scriptlanguage; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.scriptlanguage AS ENUM (
    'PYTHON',
    'JAVASCRIPT',
    'TYPESCRIPT',
    'BASH'
);


--
-- Name: skilldisclosuremode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.skilldisclosuremode AS ENUM (
    'PROGRESSIVE',
    'EXECUTE',
    'BOTH'
);


--
-- Name: skillresourcetype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.skillresourcetype AS ENUM (
    'REFERENCE',
    'FORM',
    'SCHEMA',
    'TEMPLATE',
    'EXAMPLE',
    'ASSET'
);


--
-- Name: skillstatus; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.skillstatus AS ENUM (
    'DRAFT',
    'VALIDATED',
    'PUBLISHED',
    'DEPRECATED',
    'ARCHIVED'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: a2a_push_notification_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.a2a_push_notification_configs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    task_id character varying(64) NOT NULL,
    config_json json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: a2a_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.a2a_tasks (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    resource_group_id character varying(255),
    context_id character varying(64),
    state character varying(32) NOT NULL,
    status_timestamp timestamp with time zone DEFAULT now() NOT NULL,
    task_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: action_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.action_records (
    record_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    log_id character varying(64) NOT NULL,
    seq bigint NOT NULL,
    ai_system_id character varying(255),
    root_task_id character varying(255),
    proxy_log_id character varying(255),
    verdict character varying(32) NOT NULL,
    kind character varying(8) NOT NULL,
    surface character varying(32) NOT NULL,
    epoch character varying(96),
    "timestamp" timestamp with time zone NOT NULL,
    tags jsonb,
    record jsonb NOT NULL,
    statement bytea NOT NULL,
    kid character varying(64) NOT NULL,
    chain_parent_record_id character varying(64) GENERATED ALWAYS AS (((record -> 'chain'::text) ->> 'parent_record_id'::text)) STORED,
    chain_relation character varying(32) GENERATED ALWAYS AS (((record -> 'chain'::text) ->> 'relation'::text)) STORED,
    human_disposed boolean GENERATED ALWAYS AS (COALESCE((((record -> 'disposition'::text) -> 'human_disposed'::text) = 'true'::jsonb), false)) STORED,
    backfilled boolean GENERATED ALWAYS AS (COALESCE((((record -> 'context'::text) -> 'backfilled'::text) = 'true'::jsonb), false)) STORED,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: admin_stream_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_stream_tickets (
    ticket_hash character varying(64) NOT NULL,
    stream character varying(64) NOT NULL,
    admin_id character varying(255) NOT NULL,
    admin_type character varying(32) NOT NULL,
    tenant_id character varying(255),
    session_claims json NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_card_capabilities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_card_capabilities (
    id character varying(255) NOT NULL,
    agent_card_id character varying(255) NOT NULL,
    capability_name character varying(255) NOT NULL,
    description text,
    input_schema json,
    output_schema json,
    timeout_secs integer,
    rate_limit_per_hour integer,
    data_classification json,
    handler_type character varying(32),
    handler_ref character varying(255),
    extensions jsonb,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_cards (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    version character varying(64) DEFAULT '1.0.0'::character varying NOT NULL,
    description text,
    source_url character varying(1024),
    icon_url character varying(2048),
    card_json json NOT NULL,
    supported_interfaces jsonb,
    signatures jsonb,
    extensions jsonb,
    protocol_version character varying(8) DEFAULT '0.3'::character varying NOT NULL,
    extended_card_json jsonb,
    tenant_limits json,
    tenant_governance json,
    signature text,
    signature_algorithm character varying(32),
    signed_by character varying(255),
    signed_at timestamp with time zone,
    trust_tier character varying(32) DEFAULT 'unsigned'::character varying NOT NULL,
    revoked_at timestamp with time zone,
    revoked_reason text,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    health_status character varying(32) DEFAULT 'unknown'::character varying NOT NULL,
    last_health_check_at timestamp with time zone,
    health_check_error text,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: agent_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_grants (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    agent_id character varying(255) NOT NULL,
    action_type character varying(16) NOT NULL,
    target character varying(512) DEFAULT '*'::character varying NOT NULL,
    effect character varying(32) NOT NULL,
    constraints jsonb,
    enabled boolean DEFAULT true NOT NULL,
    expires_at timestamp with time zone,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_identities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_identities (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    owner_user_id integer,
    owner_label character varying(255),
    idp_server_id character varying(255),
    idp_subject character varying(512),
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    revoked_at timestamp with time zone,
    expires_at timestamp with time zone,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    default_deny boolean DEFAULT true NOT NULL,
    enforcement_mode character varying(16) DEFAULT 'enforce'::character varying NOT NULL,
    autonomy_level character varying(24),
    implementation_name character varying(64),
    approved_versions jsonb DEFAULT '[]'::jsonb NOT NULL,
    require_release boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: agent_identity_approval_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_identity_approval_history (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    agent_id character varying(255) NOT NULL,
    approved_versions jsonb NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_to timestamp with time zone,
    changed_by character varying(255),
    source character varying(32) NOT NULL,
    CONSTRAINT ck_agent_identity_approval_history_period CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
    CONSTRAINT ck_agent_identity_approval_history_source CHECK (((source)::text = ANY ((ARRAY['declaration'::character varying, 'backfill_contract'::character varying, 'backfill_live'::character varying])::text[])))
);


--
-- Name: agent_implementation_releases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_implementation_releases (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    agent_id character varying(255) NOT NULL,
    name character varying(64) NOT NULL,
    version character varying(64) NOT NULL,
    build character varying(128) DEFAULT ''::character varying NOT NULL,
    trust character varying(16) NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    run_count integer DEFAULT 0 NOT NULL,
    fingerprints jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT ck_agent_implementation_releases_trust CHECK (((trust)::text = ANY ((ARRAY['verified'::character varying, 'declared'::character varying])::text[])))
);


--
-- Name: agent_skill_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_assets (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    skill_id character varying(255) NOT NULL,
    asset_type public.assettype NOT NULL,
    filename character varying(255) NOT NULL,
    description text,
    content text NOT NULL,
    content_hash character varying(64) NOT NULL,
    mime_type character varying(100),
    size_bytes integer,
    encoding character varying(20) NOT NULL,
    vars_schema json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_skill_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_dependencies (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    parent_skill_id character varying(255) NOT NULL,
    dependency_skill_id character varying(255) NOT NULL,
    dependency_type public.dependencytype NOT NULL,
    execution_order integer NOT NULL,
    condition json,
    description character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: agent_skill_executions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_executions (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    skill_id character varying(255),
    group_id character varying(255),
    user_id integer,
    skill_name character varying(64),
    skill_version character varying(50),
    group_name character varying(255),
    username character varying(255),
    request_id character varying(255) NOT NULL,
    correlation_id character varying(255),
    parent_execution_id character varying(255),
    execution_status public.executionstatus NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    running_at timestamp with time zone,
    completed_at timestamp with time zone,
    duration_ms integer,
    mcp_servers_used json,
    llm_models_used json,
    total_tokens integer,
    total_mcp_calls integer,
    estimated_cost_usd double precision,
    approval_status public.approvalstatus NOT NULL,
    approved_by character varying(255),
    approved_at timestamp with time zone,
    approval_notes text,
    approval_expires_at timestamp with time zone,
    error_type character varying(100),
    error_message text,
    error_details json,
    execution_context json,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_skill_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_resources (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    skill_id character varying(255) NOT NULL,
    resource_type public.skillresourcetype NOT NULL,
    filename character varying(255) NOT NULL,
    content text NOT NULL,
    content_hash character varying(64) NOT NULL,
    mime_type character varying(100),
    size_bytes integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_skill_scripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_scripts (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    skill_id character varying(255) NOT NULL,
    filename character varying(255) NOT NULL,
    description text,
    script_content text NOT NULL,
    script_hash character varying(64) NOT NULL,
    language public.scriptlanguage NOT NULL,
    execution_mode public.scriptexecutionmode NOT NULL,
    args_schema json,
    approved_by character varying(255),
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skills (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(64) NOT NULL,
    description character varying(1024) NOT NULL,
    license character varying(100),
    compatibility json,
    allowed_tools json,
    tenant_limits json,
    tenant_governance json,
    skill_md_content text NOT NULL,
    skill_md_hash character varying(64) NOT NULL,
    metadata json,
    version character varying(50) NOT NULL,
    status public.skillstatus NOT NULL,
    enabled boolean NOT NULL,
    allow_local_export boolean DEFAULT false NOT NULL,
    disclosure_mode public.skilldisclosuremode DEFAULT 'PROGRESSIVE'::public.skilldisclosuremode NOT NULL,
    published_at timestamp with time zone,
    published_by character varying(255),
    deprecated_at timestamp with time zone,
    deprecation_reason text,
    validation_errors json,
    last_validated_at timestamp with time zone,
    execution_count integer NOT NULL,
    last_executed_at timestamp with time zone,
    workflow_definition json,
    workflow_executions json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source_repo_id character varying(255),
    source_commit_sha character varying(64),
    source_path character varying(1024),
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: ai_asset_factsheets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_asset_factsheets (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    asset_id character varying(255) NOT NULL,
    period_hours integer NOT NULL,
    snapshot_json jsonb NOT NULL,
    trigger character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_assets (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    asset_kind character varying(32) NOT NULL,
    ref_id character varying(255) NOT NULL,
    display_name character varying(255),
    icon_url character varying(1024),
    source_enabled boolean,
    observed boolean,
    owner character varying(255),
    lifecycle_stage character varying(32) DEFAULT 'active'::character varying NOT NULL,
    risk_tier character varying(32),
    intended_use text,
    gpai_classification character varying(24),
    operated_by character varying(16),
    enforcement_mode character varying(16) DEFAULT 'off'::character varying NOT NULL,
    tags jsonb,
    status character varying(32) DEFAULT 'ok'::character varying NOT NULL,
    source character varying(16) DEFAULT 'discovered'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_assurance_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_assurance_reports (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    period_hours integer NOT NULL,
    report_json jsonb NOT NULL,
    health_state character varying(16),
    contract_version integer,
    contract_hash character varying(80),
    trigger character varying(24) DEFAULT 'manual'::character varying NOT NULL,
    captured_by character varying(255),
    signature_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_baseline_epochs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_baseline_epochs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    metric_key character varying(64) NOT NULL,
    basis character varying(16) DEFAULT 'runs'::character varying NOT NULL,
    caller_system_id character varying(255) DEFAULT 'all'::character varying NOT NULL,
    bucket character varying(128) DEFAULT 'all'::character varying NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    kind character varying(32) NOT NULL,
    drift_event_id character varying(64),
    measurement_change_id character varying(64),
    reason text,
    declared_by character varying(255),
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    record_id character varying(64),
    sealed_at timestamp with time zone,
    CONSTRAINT ck_ai_system_baseline_epochs_basis CHECK (((basis)::text = ANY ((ARRAY['runs'::character varying, 'segments'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_baseline_epochs_kind CHECK (((kind)::text = ANY ((ARRAY['accepted_change'::character varying, 'false_positive'::character varying, 'measurement_change'::character varying, 'catalogue_change'::character varying])::text[])))
);


--
-- Name: ai_system_baselines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_baselines (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    metric_key character varying(64) NOT NULL,
    bucket character varying(128) DEFAULT 'all'::character varying NOT NULL,
    actor_id character varying(255) DEFAULT 'all'::character varying NOT NULL,
    basis character varying(16) DEFAULT 'runs'::character varying NOT NULL,
    caller_system_id character varying(255) DEFAULT 'all'::character varying NOT NULL,
    median double precision,
    mad double precision,
    p50 double precision,
    p90 double precision,
    p99 double precision,
    distribution jsonb,
    sample_count integer DEFAULT 0 NOT NULL,
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    state character varying(16) DEFAULT 'learning'::character varying NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ai_system_baselines_basis CHECK (((basis)::text = ANY ((ARRAY['runs'::character varying, 'segments'::character varying])::text[])))
);


--
-- Name: ai_system_compliance_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_compliance_profiles (
    ai_system_id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    core jsonb DEFAULT '{}'::jsonb NOT NULL,
    frameworks jsonb DEFAULT '{}'::jsonb NOT NULL,
    declaration_record_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: ai_system_contracts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_contracts (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    version integer NOT NULL,
    contract_hash character varying(80) NOT NULL,
    resolved_closure jsonb NOT NULL,
    status character varying(16) DEFAULT 'draft'::character varying NOT NULL,
    diff_from_version integer,
    diff_summary jsonb,
    lifecycle_stage_at_promotion character varying(32),
    dry_run_summary jsonb,
    replay_run_id character varying(64),
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_by character varying(255),
    approved_at timestamp with time zone,
    promoted_at timestamp with time zone,
    rejection_reason text
);


--
-- Name: ai_system_data_scopes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_data_scopes (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    mode character varying(8) DEFAULT 'audit'::character varying NOT NULL,
    on_unbound character varying(8) DEFAULT 'deny'::character varying NOT NULL,
    keys jsonb DEFAULT '[]'::jsonb NOT NULL,
    rules jsonb DEFAULT '[]'::jsonb NOT NULL,
    unscoped_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(255),
    CONSTRAINT ck_data_scope_mode CHECK (((mode)::text = ANY ((ARRAY['off'::character varying, 'audit'::character varying, 'enforce'::character varying])::text[]))),
    CONSTRAINT ck_data_scope_on_unbound CHECK (((on_unbound)::text = ANY ((ARRAY['deny'::character varying, 'allow'::character varying])::text[])))
);


--
-- Name: ai_system_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_dependencies (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    role character varying(32) NOT NULL,
    depends_on_system_id character varying(255),
    agent_card_id character varying(255),
    criticality character varying(16) DEFAULT 'critical'::character varying NOT NULL,
    fallback text,
    contributes_to_decision boolean DEFAULT true NOT NULL,
    separability_rationale text,
    separability_record_id character varying(64),
    separability_sealed_at timestamp with time zone,
    contract_pin character varying(64),
    supplier_legal_entity character varying(255),
    assumptions jsonb DEFAULT '{}'::jsonb NOT NULL,
    expected_outcomes jsonb,
    declared_by character varying(255),
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(255),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ai_system_dependencies_criticality CHECK (((criticality)::text = ANY ((ARRAY['critical'::character varying, 'degradable'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_dependencies_no_self_edge CHECK (((ai_system_id)::text <> (depends_on_system_id)::text)),
    CONSTRAINT ck_ai_system_dependencies_role CHECK (((role)::text = ANY ((ARRAY['shared_service'::character varying, 'external'::character varying])::text[])))
);


--
-- Name: ai_system_drift_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_drift_events (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    actor_id character varying(255),
    basis character varying(16) DEFAULT 'runs'::character varying NOT NULL,
    caller_system_id character varying(255),
    drift_class character varying(24) NOT NULL,
    metric_key character varying(64) NOT NULL,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    change_point_at timestamp with time zone,
    baseline_value double precision,
    observed_value double precision,
    effect_size double precision,
    direction character varying(8),
    severity character varying(16) DEFAULT 'medium'::character varying NOT NULL,
    attributed_cause character varying(32) DEFAULT 'unexplained'::character varying NOT NULL,
    cause_confidence double precision,
    cause_evidence jsonb,
    correlated_metrics jsonb,
    response_taken jsonb,
    system_alert_id character varying(64),
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    resolution_note text,
    resolved_by character varying(255),
    resolved_at timestamp with time zone,
    resolution_kind character varying(32),
    CONSTRAINT ck_ai_system_drift_events_basis CHECK (((basis)::text = ANY ((ARRAY['runs'::character varying, 'segments'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_drift_events_open_no_kind CHECK ((((status)::text <> ALL ((ARRAY['active'::character varying, 'acknowledged'::character varying])::text[])) OR (resolution_kind IS NULL))),
    CONSTRAINT ck_ai_system_drift_events_resolution_kind CHECK (((resolution_kind IS NULL) OR ((resolution_kind)::text = ANY ((ARRAY['accepted_change'::character varying, 'cause_fixed'::character varying, 'false_positive'::character varying, 'auto_cleared'::character varying])::text[]))))
);


--
-- Name: ai_system_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_evidence (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    kind character varying(40) NOT NULL,
    title character varying(500) NOT NULL,
    notes text,
    uri text,
    sha256 character varying(64),
    assessor character varying(255),
    independent boolean,
    produced_at timestamp with time zone,
    valid_until timestamp with time zone,
    dependency_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: ai_system_health; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_health (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    health_state character varying(16) DEFAULT 'unknown'::character varying NOT NULL,
    health_score double precision,
    liveness_score double precision,
    behaviour_score double precision,
    reliability_score double precision,
    cost_score double precision,
    conformance_score double precision,
    oversight_score double precision,
    review_score double precision,
    chain_integrity_pct double precision,
    baseline_state character varying(16),
    verdict_status character varying(32),
    t_class character varying(12),
    k_median double precision,
    a_max integer,
    runs_24h integer DEFAULT 0 NOT NULL,
    last_run_at timestamp with time zone,
    open_drift_events integer DEFAULT 0 NOT NULL,
    reasons jsonb,
    dependency_cap jsonb,
    recent_runs jsonb,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_incident_window_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_incident_window_findings (
    incident_window_id character varying(64) NOT NULL,
    drift_event_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    role character varying(16) NOT NULL,
    attached_by character varying(255),
    attached_at timestamp with time zone DEFAULT now() NOT NULL,
    record_id character varying(64),
    sealed_at timestamp with time zone,
    CONSTRAINT ck_ai_system_incident_window_findings_role CHECK (((role)::text = ANY ((ARRAY['declared'::character varying, 'attached'::character varying])::text[])))
);


--
-- Name: ai_system_incident_windows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_incident_windows (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255),
    resource_type character varying(32),
    resource_id character varying(255),
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    cause_category character varying(32) NOT NULL,
    reason text NOT NULL,
    drift_event_id character varying(64),
    declared_by character varying(255),
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(255),
    updated_at timestamp with time zone,
    record_id character varying(64),
    sealed_at timestamp with time zone,
    CONSTRAINT ck_ai_system_incident_windows_bounds CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_ai_system_incident_windows_cause CHECK (((cause_category)::text = ANY ((ARRAY['provider_outage'::character varying, 'platform_incident'::character varying, 'maintenance'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_incident_windows_resource CHECK (((resource_type IS NULL) = (resource_id IS NULL))),
    CONSTRAINT ck_ai_system_incident_windows_resource_type CHECK (((resource_type IS NULL) OR ((resource_type)::text = ANY ((ARRAY['model'::character varying, 'provider'::character varying, 'mcp_server'::character varying, 'skill'::character varying, 'knowledge_base'::character varying, 'agent'::character varying])::text[]))))
);


--
-- Name: ai_system_lifecycle_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_lifecycle_events (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    event_type character varying(32) DEFAULT 'stage_transition'::character varying NOT NULL,
    from_stage character varying(32),
    to_stage character varying(32),
    subject_id character varying(64),
    details jsonb,
    approved_by character varying(255),
    recorded_by character varying(255) NOT NULL,
    recorded_by_id character varying(255),
    successor_system_id character varying(255),
    no_successor boolean DEFAULT false NOT NULL,
    note text,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_ai_system_lifecycle_events_stage CHECK ((((event_type)::text <> 'stage_transition'::text) OR (to_stage IS NOT NULL))),
    CONSTRAINT ck_ai_system_lifecycle_events_type CHECK (((event_type)::text = ANY ((ARRAY['stage_transition'::character varying, 'agent.new_release'::character varying])::text[])))
);


--
-- Name: ai_system_liveness_expectations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_liveness_expectations (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    mode character varying(16) DEFAULT 'on_demand'::character varying NOT NULL,
    expected_min_runs integer DEFAULT 1 NOT NULL,
    window_seconds integer DEFAULT 3600 NOT NULL,
    grace_seconds integer DEFAULT 300 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    max_quiet_processed_seconds integer,
    learned_from_runs integer,
    learned_median_gap_seconds integer,
    learned_p95_gap_seconds integer,
    learned_at timestamp with time zone,
    accepted_from_learned boolean DEFAULT false NOT NULL,
    last_seen_run_at timestamp with time zone,
    last_checked_at timestamp with time zone,
    last_alert_at timestamp with time zone,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_reach_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_reach_rules (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    scope_key character varying(64) NOT NULL,
    group_by character varying(72) NOT NULL,
    window_hours integer DEFAULT 24 NOT NULL,
    max_distinct integer NOT NULL,
    severity character varying(8) DEFAULT 'high'::character varying NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    CONSTRAINT ck_reach_max CHECK ((max_distinct >= 1)),
    CONSTRAINT ck_reach_severity CHECK (((severity)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT ck_reach_window CHECK (((window_hours >= 1) AND (window_hours <= 720)))
);


--
-- Name: ai_system_record_access; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_record_access (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    root_task_id character varying(255) NOT NULL,
    correlation_id character varying(255),
    tool character varying(255) NOT NULL,
    scope_key character varying(64) NOT NULL,
    record_ref character varying(64),
    verdict character varying(20) NOT NULL,
    mode character varying(8) NOT NULL,
    subject_id character varying(255),
    api_key_id character varying(255),
    agent_id character varying(255),
    accessed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_replay_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_replay_cases (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    suite_id character varying(64) NOT NULL,
    source_run_id character varying(64),
    source_root_task_id character varying(255),
    recorded_request jsonb NOT NULL,
    recorded_trajectory jsonb NOT NULL,
    recorded_terminal_state character varying(32),
    recorded_action_count integer,
    recorded_tool_signature character varying(512),
    stratum character varying(64),
    side_effect_policy character varying(16) DEFAULT 'mock'::character varying NOT NULL,
    criticality character varying(16) DEFAULT 'normal'::character varying NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_replay_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_replay_runs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    suite_id character varying(64) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    contract_id character varying(64),
    status character varying(16) DEFAULT 'running'::character varying NOT NULL,
    triggered_by character varying(24) DEFAULT 'manual'::character varying NOT NULL,
    triggered_by_actor character varying(255),
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    total_cases integer DEFAULT 0 NOT NULL,
    matched integer DEFAULT 0 NOT NULL,
    shape_changed integer DEFAULT 0 NOT NULL,
    policy_changed integer DEFAULT 0 NOT NULL,
    outcome_changed integer DEFAULT 0 NOT NULL,
    failed integer DEFAULT 0 NOT NULL,
    skipped integer DEFAULT 0 NOT NULL,
    pass_rate double precision,
    critical_outcome_changed integer DEFAULT 0 NOT NULL,
    case_results jsonb,
    error_message text
);


--
-- Name: ai_system_replay_suites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_replay_suites (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    pass_threshold double precision DEFAULT '0.9'::double precision NOT NULL,
    sampling_strategy character varying(16) DEFAULT 'stratified'::character varying NOT NULL,
    target_case_count integer DEFAULT 30 NOT NULL,
    replayable boolean DEFAULT true NOT NULL,
    not_replayable_reason text,
    enabled boolean DEFAULT true NOT NULL,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_response_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_response_policies (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    signal_kind character varying(16) DEFAULT 'drift'::character varying NOT NULL,
    drift_class character varying(24),
    severity_at_least character varying(16) DEFAULT 'high'::character varying NOT NULL,
    actions jsonb NOT NULL,
    allow_suspend boolean DEFAULT false NOT NULL,
    require_human_to_restore boolean DEFAULT true NOT NULL,
    response_cooldown_seconds integer DEFAULT 3600 NOT NULL,
    last_fired_at timestamp with time zone,
    last_fired_drift_event_id character varying(64),
    fire_count integer DEFAULT 0 NOT NULL,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_run_feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_run_feedback (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    root_task_id character varying(255) NOT NULL,
    user_id character varying(255) NOT NULL,
    score smallint NOT NULL,
    comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_run_scopes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_run_scopes (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    root_task_id character varying(255) NOT NULL,
    scope_key character varying(64) NOT NULL,
    scope_values jsonb NOT NULL,
    source character varying(16) DEFAULT 'declared'::character varying NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_system_run_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_run_segments (
    id character varying(64) NOT NULL,
    run_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    root_task_id character varying(255) NOT NULL,
    root_system_id character varying(255),
    segment_system_id character varying(255) NOT NULL,
    caller_system_id character varying(255),
    min_depth smallint DEFAULT '0'::smallint NOT NULL,
    first_action_at timestamp with time zone NOT NULL,
    last_action_at timestamp with time zone NOT NULL,
    active_ms integer DEFAULT 0 NOT NULL,
    action_count integer DEFAULT 0 NOT NULL,
    llm_call_count integer DEFAULT 0 NOT NULL,
    tool_call_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    tool_error_count integer DEFAULT 0 NOT NULL,
    guardrail_block_count integer DEFAULT 0 NOT NULL,
    policy_denial_count integer DEFAULT 0 NOT NULL,
    approval_request_count integer DEFAULT 0 NOT NULL,
    total_tokens bigint DEFAULT '0'::bigint NOT NULL,
    cost_usd numeric(18,8) DEFAULT '0'::numeric NOT NULL,
    models_used jsonb DEFAULT '{}'::jsonb NOT NULL,
    tool_set_signature character varying(64),
    caller_agent_id character varying(255),
    a2a_task_state character varying(32),
    outcome character varying(16) NOT NULL,
    outcome_id character varying(128),
    outcome_conformance character varying(16),
    error_cause character varying(32),
    held_approval_request_id character varying(64),
    agent_impl_name character varying(64),
    agent_impl_version character varying(64),
    agent_impl_build character varying(128),
    agent_impl_trust character varying(16),
    delegate_agent_id character varying(255),
    contract_id character varying(64),
    contract_hash character varying(80),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ai_system_run_segments_agent_impl_trust CHECK (((agent_impl_trust)::text = ANY ((ARRAY['verified'::character varying, 'declared'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_run_segments_outcome CHECK (((outcome)::text = ANY ((ARRAY['answered'::character varying, 'failed'::character varying, 'rejected'::character varying, 'timed_out'::character varying])::text[])))
);


--
-- Name: ai_system_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_system_runs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    root_task_id character varying(255) NOT NULL,
    ai_system_id character varying(255),
    resource_group_id character varying(255),
    via_system_ids jsonb,
    system_kind character varying(24),
    actor_type character varying(24),
    actor_id character varying(255),
    agent_id character varying(255),
    subject_id character varying(255),
    subject_kind character varying(16),
    ai_asset_id character varying(255),
    contract_id character varying(64),
    contract_hash character varying(80),
    trace_root_kind character varying(32),
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone,
    duration_ms integer,
    awaiting_human_ms integer DEFAULT 0 NOT NULL,
    active_duration_ms integer DEFAULT 0 NOT NULL,
    terminal_state character varying(24) NOT NULL,
    finalized_by character varying(16) NOT NULL,
    action_count integer DEFAULT 0 NOT NULL,
    llm_call_count integer DEFAULT 0 NOT NULL,
    tool_call_count integer DEFAULT 0 NOT NULL,
    skill_call_count integer DEFAULT 0 NOT NULL,
    a2a_call_count integer DEFAULT 0 NOT NULL,
    turn_count integer DEFAULT 0 NOT NULL,
    turn_attribution character varying(16) DEFAULT 'inferred'::character varying NOT NULL,
    step_count integer DEFAULT 0 NOT NULL,
    max_delegation_depth integer DEFAULT 0 NOT NULL,
    distinct_tools integer DEFAULT 0 NOT NULL,
    tools_used jsonb,
    scope_declared boolean,
    records_touched integer,
    scope_violation_count integer,
    scope_unbound_count integer,
    tool_signature character varying(64),
    tool_set_signature character varying(64),
    total_tokens integer DEFAULT 0 NOT NULL,
    prompt_tokens integer DEFAULT 0 NOT NULL,
    completion_tokens integer DEFAULT 0 NOT NULL,
    total_cost_usd numeric(18,8) DEFAULT '0'::numeric NOT NULL,
    models_used jsonb,
    cache_hit_count integer DEFAULT 0 NOT NULL,
    tokens_saved integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    fallback_count integer DEFAULT 0 NOT NULL,
    tool_error_count integer DEFAULT 0 NOT NULL,
    tool_error_types jsonb,
    guardrail_trip_count integer DEFAULT 0 NOT NULL,
    guardrail_block_count integer DEFAULT 0 NOT NULL,
    policy_denial_count integer DEFAULT 0 NOT NULL,
    approval_request_count integer DEFAULT 0 NOT NULL,
    input_bytes integer DEFAULT 0 NOT NULL,
    output_bytes integer DEFAULT 0 NOT NULL,
    first_prompt_tokens integer,
    peak_context_tokens integer,
    peak_context_utilization double precision,
    context_growth double precision,
    client_label character varying(255),
    client_user_agent_hash character varying(64),
    agent_impl_name character varying(64),
    agent_impl_version character varying(64),
    agent_impl_build character varying(128),
    agent_impl_trust character varying(16),
    implementation_fingerprint character varying(71),
    rag_used boolean DEFAULT false NOT NULL,
    rag_chunks integer DEFAULT 0 NOT NULL,
    prompt_fingerprint character varying(64),
    rag_collection_ids character varying(1024),
    outcome_id character varying(128),
    outcome_conformance character varying(16),
    outcome_counts_toward_baseline boolean,
    outcome_contract_version integer,
    error_cause character varying(32),
    chain_integrity character varying(24) DEFAULT 'intact'::character varying NOT NULL,
    reopened_count integer DEFAULT 0 NOT NULL,
    source character varying(16) DEFAULT 'gateway'::character varying NOT NULL,
    is_replay boolean DEFAULT false NOT NULL,
    feedback_score smallint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ai_system_runs_agent_impl_trust CHECK (((agent_impl_trust)::text = ANY ((ARRAY['verified'::character varying, 'declared'::character varying])::text[])))
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    key_hash character varying(255) NOT NULL,
    key_prefix character varying(50) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    scope character varying(20) NOT NULL,
    allowed_skill_ids json,
    skill_rate_limit_daily integer,
    user_id integer,
    user_email character varying(255),
    agent_id character varying(255),
    group_id character varying(255),
    expires_at timestamp with time zone,
    enabled boolean NOT NULL,
    is_platform_service boolean DEFAULT false NOT NULL,
    revoked boolean NOT NULL,
    revoked_at timestamp with time zone,
    revoked_by character varying(255),
    revoked_reason text,
    last_used_at timestamp with time zone,
    last_used_by_user_id integer,
    usage_count integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(255)
);


--
-- Name: argument_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.argument_policies (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(1024),
    surface character varying(64) NOT NULL,
    target character varying(255) NOT NULL,
    argument_key character varying(255) NOT NULL,
    analyzer character varying(32) NOT NULL,
    analyzer_config jsonb,
    rules jsonb DEFAULT '[]'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    severity character varying(32) DEFAULT 'deny'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_argument_policies_analyzer CHECK (((analyzer)::text = ANY ((ARRAY['sql'::character varying, 'url'::character varying, 'shell'::character varying, 'path'::character varying, 'json'::character varying])::text[]))),
    CONSTRAINT ck_argument_policies_severity CHECK (((severity)::text = ANY ((ARRAY['deny'::character varying, 'warn'::character varying, 'approval_required'::character varying])::text[]))),
    CONSTRAINT ck_argument_policies_surface CHECK (((surface)::text = ANY ((ARRAY['llm_output_tool_calls'::character varying, 'mcp_input'::character varying, 'skill_input'::character varying])::text[])))
);


--
-- Name: assurance_check_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_check_results (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    check_id character varying(64) NOT NULL,
    check_version integer NOT NULL,
    run_id character varying(64),
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    value jsonb NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    evaluator_fingerprint character varying(64) NOT NULL,
    backtest boolean DEFAULT false NOT NULL,
    tokens_used integer,
    evaluated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: assurance_check_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_check_reviews (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    check_id character varying(64) NOT NULL,
    evaluator_fingerprint character varying(64) NOT NULL,
    week date NOT NULL,
    sample_key character varying(255) NOT NULL,
    assessment_record_id character varying(64),
    judged_result character varying(16) NOT NULL,
    judged_score numeric(10,4),
    human_result character varying(16),
    human_score numeric(10,4),
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    blind boolean DEFAULT true NOT NULL,
    reviewer_id character varying(255),
    reviewed_at timestamp with time zone,
    adjudication_record_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: assurance_check_rubrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_check_rubrics (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    check_id character varying(64) NOT NULL,
    rubric_digest character varying(64) NOT NULL,
    rubric jsonb NOT NULL,
    period_start date NOT NULL,
    period_end date,
    record_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: assurance_check_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_check_versions (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    check_id character varying(64) NOT NULL,
    version integer NOT NULL,
    spec_hash character varying(64) NOT NULL,
    spec jsonb NOT NULL,
    backtest_summary jsonb,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: assurance_checks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_checks (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description_nl text,
    version integer DEFAULT 1 NOT NULL,
    spec_hash character varying(64) NOT NULL,
    scope jsonb DEFAULT '{}'::jsonb NOT NULL,
    tier character varying(16) NOT NULL,
    evaluator jsonb NOT NULL,
    output character varying(24) NOT NULL,
    trigger character varying(16) NOT NULL,
    threshold jsonb,
    route_to_inbox boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    created_by character varying(255),
    approved_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_achk_output CHECK (((output)::text = ANY ((ARRAY['boolean'::character varying, 'classification'::character varying, 'score'::character varying, 'count'::character varying, 'extraction'::character varying])::text[]))),
    CONSTRAINT ck_achk_tier CHECK (((tier)::text = ANY ((ARRAY['deterministic'::character varying, 'semantic'::character varying])::text[]))),
    CONSTRAINT ck_achk_trigger CHECK (((trigger)::text = ANY ((ARRAY['on_finalize'::character varying, 'batch'::character varying])::text[])))
);


--
-- Name: assurance_digests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_digests (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    week_start date NOT NULL,
    content jsonb NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: assurance_inbox_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_inbox_events (
    id character varying(64) NOT NULL,
    item_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    admin character varying(255),
    event character varying(32) NOT NULL,
    detail jsonb,
    CONSTRAINT ck_inbox_event CHECK (((event)::text = ANY ((ARRAY['created'::character varying, 'reoccurred'::character varying, 'acknowledged'::character varying, 'assigned'::character varying, 'resolved'::character varying, 'reopened'::character varying])::text[])))
);


--
-- Name: assurance_inbox_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assurance_inbox_items (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255),
    category character varying(32) NOT NULL,
    severity character varying(16) NOT NULL,
    fingerprint character varying(512) NOT NULL,
    title character varying(500) NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    occurrence_count integer DEFAULT 1 NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    state character varying(16) DEFAULT 'open'::character varying NOT NULL,
    acknowledged_at timestamp with time zone,
    acknowledged_by character varying(255),
    assignee_admin character varying(255),
    disposition character varying(32),
    resolution_note text,
    resolved_at timestamp with time zone,
    resolved_by character varying(255),
    CONSTRAINT ck_inbox_disposition CHECK (((disposition IS NULL) OR ((disposition)::text = ANY ((ARRAY['true_positive'::character varying, 'false_positive'::character varying, 'expected'::character varying, 'fixed'::character varying])::text[])))),
    CONSTRAINT ck_inbox_severity CHECK (((severity)::text = ANY ((ARRAY['info'::character varying, 'warning'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT ck_inbox_state CHECK (((state)::text = ANY ((ARRAY['open'::character varying, 'acknowledged'::character varying, 'resolved'::character varying])::text[])))
);


--
-- Name: audit_chain_checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_chain_checkpoints (
    id character varying(64) NOT NULL,
    chain_id character varying(64) NOT NULL,
    seq_start bigint NOT NULL,
    seq_end bigint NOT NULL,
    head_hash character varying(64) NOT NULL,
    signature character varying(128) NOT NULL,
    public_key character varying(64) NOT NULL,
    signed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: audit_chain_prunes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_chain_prunes (
    id character varying(64) NOT NULL,
    chain_id character varying(64) NOT NULL,
    tenant_id character varying(255),
    seq_min bigint NOT NULL,
    seq_max bigint NOT NULL,
    row_count integer NOT NULL,
    runs jsonb NOT NULL,
    reason character varying(16) NOT NULL,
    actor character varying(255),
    pruned_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: audit_export_destinations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_export_destinations (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    destination_type character varying(32) NOT NULL,
    enabled boolean NOT NULL,
    config json NOT NULL,
    field_allowlist json,
    interval_seconds integer NOT NULL,
    last_exported_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_run_status character varying(32),
    last_run_rows integer,
    last_run_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: batch_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.batch_jobs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    resource_group_id character varying(255),
    resource_group_name character varying(255),
    user_id integer,
    username character varying(255),
    api_key_id character varying(255),
    agent_id character varying(255),
    llm_model_id character varying(255),
    llm_model_name character varying(255),
    provider character varying(100) NOT NULL,
    model character varying(255) NOT NULL,
    endpoint character varying(255) NOT NULL,
    completion_window character varying(50) NOT NULL,
    provider_batch_id character varying(500),
    provider_input_file_id character varying(500),
    provider_output_file_id character varying(500),
    provider_error_file_id character varying(500),
    description text,
    custom_metadata json,
    status character varying(50) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    submitted_at timestamp with time zone,
    validating_at timestamp with time zone,
    in_progress_at timestamp with time zone,
    completed_at timestamp with time zone,
    expires_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    cancelled_by character varying(255),
    root_task_id character varying(255),
    submit_task_id character varying(255),
    delegation_depth integer,
    total_requests integer NOT NULL,
    completed_requests integer NOT NULL,
    failed_requests integer NOT NULL,
    total_input_tokens integer,
    total_output_tokens integer,
    estimated_cost_usd double precision,
    error_type character varying(100),
    error_message text,
    error_details json
);


--
-- Name: batch_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.batch_requests (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    batch_job_id character varying(255) NOT NULL,
    custom_id character varying(500) NOT NULL,
    request_index integer,
    status character varying(50) NOT NULL,
    request_body json,
    model_requested character varying(255),
    response_status_code integer,
    response_body json,
    input_tokens integer,
    output_tokens integer,
    estimated_cost_usd double precision,
    error_type character varying(100),
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: collector_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collector_keys (
    key_id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    collector_id character varying(255) NOT NULL,
    collector_type character varying(64),
    public_key text NOT NULL,
    trust_tier character varying(16) DEFAULT 'customer'::character varying NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    revoked_at timestamp with time zone,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


--
-- Name: compliance_documentation_exports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_documentation_exports (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255) NOT NULL,
    framework_id character varying(64) NOT NULL,
    template character varying(64) NOT NULL,
    contract_version integer,
    content jsonb NOT NULL,
    digest character varying(64) NOT NULL,
    record_id character varying(64),
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: compliance_frameworks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_frameworks (
    id character varying(64) NOT NULL,
    tag_key character varying(64),
    title character varying(255) NOT NULL,
    kind character varying(16) NOT NULL,
    jurisdiction character varying(64) NOT NULL,
    reference text NOT NULL,
    catalog_version character varying(32) DEFAULT '2026-09'::character varying NOT NULL,
    verified_against_instrument boolean DEFAULT false NOT NULL,
    retention_floor_days integer,
    deadline_rules jsonb DEFAULT '{}'::jsonb NOT NULL,
    sort_order integer DEFAULT 100 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: compliance_incident_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_incident_events (
    id character varying(64) NOT NULL,
    incident_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    event character varying(16) NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id character varying(255),
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    record_id character varying(64)
);


--
-- Name: compliance_incidents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_incidents (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255),
    title character varying(255) NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    classification character varying(64) NOT NULL,
    severity character varying(16) NOT NULL,
    became_aware_at timestamp with time zone NOT NULL,
    classified_at timestamp with time zone,
    frameworks jsonb DEFAULT '[]'::jsonb NOT NULL,
    deadlines jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    reported_at timestamp with time zone,
    authority character varying(255),
    external_reference character varying(255),
    linked_run_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    linked_inbox_item_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    stage_submissions jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_record_id character varying(64),
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: compliance_notice_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_notice_configs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    ai_system_id character varying(255),
    enabled boolean DEFAULT true NOT NULL,
    surface character varying(16) DEFAULT 'both'::character varying NOT NULL,
    texts jsonb DEFAULT '{}'::jsonb NOT NULL,
    text_digests jsonb DEFAULT '{}'::jsonb NOT NULL,
    exemption jsonb,
    record_id character varying(64),
    updated_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: compliance_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_reports (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    framework_id character varying(64) NOT NULL,
    ai_system_id character varying(255),
    cadence character varying(16) NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    catalog_version character varying(32) NOT NULL,
    status character varying(16) DEFAULT 'generating'::character varying NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    rows jsonb DEFAULT '[]'::jsonb NOT NULL,
    error text,
    record_id character varying(64),
    bundle_id character varying(64),
    countersignatures jsonb DEFAULT '[]'::jsonb NOT NULL,
    generated_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: data_erasure_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.data_erasure_events (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    subject_fingerprint character varying(64) NOT NULL,
    subject_kinds jsonb NOT NULL,
    requested_by character varying(255) NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    dry_run boolean DEFAULT false NOT NULL,
    counts jsonb NOT NULL
);


--
-- Name: disclosure_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.disclosure_records (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    log_id character varying(64) NOT NULL,
    seq bigint,
    audience character varying(255) NOT NULL,
    selector jsonb DEFAULT '{}'::jsonb NOT NULL,
    completeness character varying(24) NOT NULL,
    disclosed_record_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    suppressed_fields jsonb DEFAULT '[]'::jsonb NOT NULL,
    bundle_digest character varying(64),
    statement bytea,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: discovered_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.discovered_assets (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    asset_key character varying(512) NOT NULL,
    asset_type character varying(32) NOT NULL,
    provider character varying(64),
    display_name character varying(512),
    model_hint character varying(255),
    endpoint_scheme character varying(16),
    endpoint_host character varying(255),
    endpoint_port integer,
    endpoint_path character varying(512),
    computed_confidence double precision DEFAULT '0'::double precision NOT NULL,
    governance_status character varying(16) DEFAULT 'unknown'::character varying NOT NULL,
    governed_kind character varying(32),
    governed_ref character varying(255),
    device_id character varying(128),
    device_name character varying(255),
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    evidence_count integer DEFAULT 0 NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: discovery_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.discovery_events (
    event_id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    collector_id character varying(255),
    collector_type character varying(64),
    collector_version character varying(64),
    trust_tier character varying(16),
    asset_key character varying(512),
    emitted_at timestamp with time zone,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    accepted boolean DEFAULT true NOT NULL,
    reject_reason character varying(255),
    payload jsonb
);


--
-- Name: discovery_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.discovery_evidence (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    discovered_asset_id character varying(255) NOT NULL,
    evidence_type character varying(32) NOT NULL,
    observed_at timestamp with time zone,
    observation text,
    passive boolean DEFAULT false NOT NULL,
    host_derived boolean DEFAULT false NOT NULL,
    collector_id character varying(255),
    trust_tier character varying(16),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: docker_deployments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.docker_deployments (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    deployment_name character varying(255) NOT NULL,
    mcp_server_id character varying(255),
    mcp_server_name character varying(255),
    image_id integer,
    image_name character varying(600) NOT NULL,
    container_id character varying(64) NOT NULL,
    container_name character varying(255) NOT NULL,
    github_repo character varying(500),
    network character varying(255) NOT NULL,
    host_port integer,
    internal_port integer NOT NULL,
    environment json,
    volumes json,
    memory_limit character varying(50) NOT NULL,
    cpu_limit double precision NOT NULL,
    restart_policy character varying(50) NOT NULL,
    deployed_by character varying(50),
    deployed_at timestamp without time zone DEFAULT now() NOT NULL,
    status character varying(50) NOT NULL,
    last_status_check timestamp without time zone,
    last_started_at timestamp without time zone,
    last_stopped_at timestamp without time zone,
    health_status character varying(50),
    consecutive_failures integer NOT NULL,
    cpu_usage_percent double precision,
    memory_usage_mb double precision,
    network_rx_mb double precision,
    network_tx_mb double precision,
    stats_updated_at timestamp without time zone
);


--
-- Name: docker_deployments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.docker_deployments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: docker_deployments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.docker_deployments_id_seq OWNED BY public.docker_deployments.id;


--
-- Name: docker_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.docker_images (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    image_id character varying(64) NOT NULL,
    repository character varying(500) NOT NULL,
    tag character varying(100) NOT NULL,
    full_name character varying(600) NOT NULL,
    size_bytes integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    build_method character varying(50) NOT NULL,
    dockerfile_content text,
    build_args json,
    build_logs text,
    github_repo character varying(500),
    github_branch character varying(100),
    build_path character varying(500),
    managed_by character varying(50),
    imported_at timestamp without time zone DEFAULT now() NOT NULL,
    last_used timestamp without time zone,
    is_active boolean NOT NULL
);


--
-- Name: docker_images_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.docker_images_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: docker_images_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.docker_images_id_seq OWNED BY public.docker_images.id;


--
-- Name: ecs_deployments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecs_deployments (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    deployment_name character varying(255) NOT NULL,
    mcp_server_id character varying(255),
    mcp_server_name character varying(255),
    cluster_name character varying(255) NOT NULL,
    task_definition_arn character varying(512) NOT NULL,
    task_arn character varying(512) NOT NULL,
    region character varying(50) NOT NULL,
    eni_id character varying(255),
    private_ip character varying(45),
    image_name character varying(512) NOT NULL,
    container_port integer DEFAULT 8000 NOT NULL,
    status character varying(50) DEFAULT 'pending'::character varying NOT NULL,
    last_status_check timestamp with time zone,
    deployed_at timestamp with time zone NOT NULL,
    stopped_at timestamp with time zone
);


--
-- Name: edition_marker; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edition_marker (
    id integer NOT NULL,
    enterprise_first_started_at timestamp with time zone NOT NULL,
    enterprise_last_started_at timestamp with time zone NOT NULL,
    enterprise_first_version character varying(50) NOT NULL,
    enterprise_last_version character varying(50) NOT NULL,
    CONSTRAINT ck_edition_marker_single_row CHECK ((id = 1))
);


--
-- Name: end_user_group_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.end_user_group_members (
    end_user_group_id character varying(64) NOT NULL,
    end_user_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL
);


--
-- Name: end_user_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.end_user_groups (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    display_name character varying(255),
    description text,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    source_external_id character varying(255),
    idp_server_id character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: end_user_revoked_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.end_user_revoked_tokens (
    jti character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    end_user_id integer NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone DEFAULT now() NOT NULL,
    reason character varying(32) DEFAULT 'logout'::character varying NOT NULL
);


--
-- Name: end_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.end_users (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    role_id character varying(50),
    primary_group_id character varying(255),
    username character varying(100) NOT NULL,
    email character varying(255),
    display_name character varying(255),
    password_hash character varying(255) NOT NULL,
    is_verified boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login timestamp with time zone,
    user_metadata json,
    avatar_url character varying(500),
    photo_data bytea,
    photo_mime_type character varying(100),
    photo_size integer,
    photo_hash character varying(64),
    totp_secret text,
    totp_enabled boolean NOT NULL,
    totp_verified_at timestamp with time zone,
    backup_codes json,
    sessions_valid_after timestamp with time zone,
    idp_server_id character varying(255),
    idp_subject character varying(500),
    idp_last_login timestamp with time zone,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    source_external_id character varying(255)
);


--
-- Name: end_users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.end_users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: end_users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.end_users_id_seq OWNED BY public.end_users.id;


--
-- Name: evidence_bundles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_bundles (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    log_id character varying(64) NOT NULL,
    selector jsonb NOT NULL,
    audience character varying(255),
    payloads_mode character varying(16) NOT NULL,
    selection character varying(24) NOT NULL,
    framework_id character varying(64),
    catalog_version character varying(32),
    obligation_key character varying(128),
    root_record_id character varying(64),
    record_count integer NOT NULL,
    bundle_digest character varying(64) NOT NULL,
    record_id character varying(64),
    disclosure_record_id character varying(64),
    bundle jsonb NOT NULL,
    size_bytes integer NOT NULL,
    requested_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: evidence_log_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_log_entries (
    log_id character varying(64) NOT NULL,
    seq bigint NOT NULL,
    kind character varying(24) NOT NULL,
    leaf_hash character varying(64) NOT NULL,
    ref_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: evidence_log_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_log_nodes (
    log_id character varying(64) NOT NULL,
    level integer NOT NULL,
    index bigint NOT NULL,
    hash character varying(64) NOT NULL
);


--
-- Name: evidence_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_logs (
    log_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    next_seq bigint DEFAULT '0'::bigint NOT NULL,
    tree_size bigint DEFAULT '0'::bigint NOT NULL,
    root character varying(64),
    kid character varying(64),
    last_tree_head_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: evidence_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_receipts (
    id character varying(64) NOT NULL,
    tree_head_id character varying(64) NOT NULL,
    witness_id character varying(64),
    kind character varying(16) NOT NULL,
    receipt bytea NOT NULL,
    leaf_index bigint,
    tree_size bigint,
    witness_iat bigint,
    verdict character varying(16) DEFAULT 'unverified'::character varying NOT NULL,
    verified_at timestamp with time zone,
    vendor_ext jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: evidence_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_settings (
    tenant_id character varying(255) NOT NULL,
    per_tenant_key boolean DEFAULT false NOT NULL,
    salt_digests_default boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(255)
);


--
-- Name: evidence_signing_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_signing_keys (
    kid character varying(64) NOT NULL,
    tenant_id character varying(255),
    encrypted_seed text NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    rotation_record_id character varying(64)
);


--
-- Name: evidence_tree_heads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evidence_tree_heads (
    id character varying(64) NOT NULL,
    log_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    tree_size bigint NOT NULL,
    root character varying(64) NOT NULL,
    prev_size bigint DEFAULT '0'::bigint NOT NULL,
    prev_root character varying(64),
    statement bytea NOT NULL,
    transparent_statement bytea,
    issued_at timestamp with time zone NOT NULL,
    stamp_seq bigint,
    grade character varying(16) DEFAULT 'standalone'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: guardrail_break_glass; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guardrail_break_glass (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    reason text NOT NULL,
    activated_by character varying(255) NOT NULL,
    activated_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    deactivated_at timestamp with time zone,
    deactivated_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: guardrail_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guardrail_configs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(1024),
    is_default boolean DEFAULT false NOT NULL,
    priority integer,
    scope_mcp_server_id character varying(255),
    scope_tool_name character varying(255),
    enabled boolean DEFAULT false NOT NULL,
    chat_input_enabled boolean DEFAULT true NOT NULL,
    chat_output_enabled boolean DEFAULT false NOT NULL,
    streaming_output_mode character varying(10) DEFAULT 'auto'::character varying NOT NULL,
    embeddings_input_enabled boolean DEFAULT true NOT NULL,
    image_gen_input_enabled boolean DEFAULT true NOT NULL,
    image_gen_output_enabled boolean DEFAULT false NOT NULL,
    audio_tts_input_enabled boolean DEFAULT true NOT NULL,
    audio_tts_output_enabled boolean DEFAULT false NOT NULL,
    audio_stt_input_enabled boolean DEFAULT true NOT NULL,
    audio_stt_output_enabled boolean DEFAULT true NOT NULL,
    video_gen_input_enabled boolean DEFAULT true NOT NULL,
    video_gen_output_enabled boolean DEFAULT false NOT NULL,
    batch_input_enabled boolean DEFAULT true NOT NULL,
    batch_output_enabled boolean DEFAULT false NOT NULL,
    moderation_input_enabled boolean DEFAULT true NOT NULL,
    mcp_input_enabled boolean DEFAULT true NOT NULL,
    mcp_output_enabled boolean DEFAULT true NOT NULL,
    mcp_registration_enabled boolean DEFAULT true NOT NULL,
    skill_input_enabled boolean DEFAULT true NOT NULL,
    skill_output_enabled boolean DEFAULT true NOT NULL,
    a2a_inbound_enabled boolean DEFAULT false NOT NULL,
    a2a_outbound_enabled boolean DEFAULT false NOT NULL,
    pii_detection_enabled boolean DEFAULT true NOT NULL,
    pii_action character varying(20),
    pii_provider character varying(32),
    pii_categories json,
    pii_threshold double precision,
    prompt_injection_enabled boolean DEFAULT true NOT NULL,
    prompt_injection_action character varying(20),
    prompt_injection_sensitivity double precision,
    prompt_injection_provider character varying(32),
    jailbreak_detection_enabled boolean DEFAULT true NOT NULL,
    jailbreak_action character varying(20),
    jailbreak_provider character varying(32),
    toxic_content_enabled boolean DEFAULT false NOT NULL,
    toxic_content_action character varying(20),
    toxic_content_threshold double precision,
    toxic_content_provider character varying(32),
    toxic_content_category_thresholds json,
    banned_words json,
    banned_patterns json,
    banned_words_match_mode character varying(16),
    pii_surfaces jsonb,
    prompt_injection_surfaces jsonb,
    jailbreak_surfaces jsonb,
    toxic_content_surfaces jsonb,
    banned_patterns_surfaces jsonb,
    banned_words_surfaces jsonb,
    secrets_detection_enabled boolean DEFAULT false NOT NULL,
    secrets_action character varying(20) DEFAULT 'redact'::character varying NOT NULL,
    secrets_categories jsonb DEFAULT '["aws_access_key", "aws_secret_key", "github_token", "openai_key", "gcp_service_account", "private_key_pem", "jwt"]'::jsonb,
    secrets_surfaces jsonb,
    secrets_threshold double precision DEFAULT 0.8,
    pii_per_surface jsonb,
    prompt_injection_per_surface jsonb,
    jailbreak_per_surface jsonb,
    toxic_content_per_surface jsonb,
    secrets_per_surface jsonb,
    banned_words_per_surface jsonb,
    banned_patterns_per_surface jsonb,
    providers json,
    max_check_timeout_ms double precision,
    fail_open boolean DEFAULT false NOT NULL,
    log_all_checks boolean,
    fail_open_overrides json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: health_check_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.health_check_configs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    dashboard_enabled boolean NOT NULL,
    dashboard_interval_seconds integer NOT NULL,
    dashboard_show_ai_models boolean NOT NULL,
    dashboard_show_mcp_servers boolean NOT NULL,
    mcp_enabled boolean NOT NULL,
    mcp_interval_seconds integer NOT NULL,
    mcp_timeout_seconds integer NOT NULL,
    mcp_retry_attempts integer NOT NULL,
    mcp_failure_threshold integer NOT NULL,
    mcp_pause_when_inactive boolean NOT NULL,
    mcp_recovery_check_interval integer NOT NULL,
    ai_enabled boolean NOT NULL,
    ai_interval_seconds integer NOT NULL,
    ai_timeout_seconds integer NOT NULL,
    ai_retry_attempts integer NOT NULL,
    ai_failure_threshold integer NOT NULL,
    ai_pause_when_inactive boolean NOT NULL,
    ai_recovery_check_interval integer NOT NULL,
    ai_test_prompt character varying(500) NOT NULL,
    ai_max_tokens integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    created_by character varying(50),
    updated_by character varying(50),
    CONSTRAINT ck_ai_interval_positive CHECK ((ai_interval_seconds > 0)),
    CONSTRAINT ck_ai_max_tokens_positive CHECK ((ai_max_tokens > 0)),
    CONSTRAINT ck_ai_recovery_positive CHECK ((ai_recovery_check_interval > 0)),
    CONSTRAINT ck_ai_retry_non_negative CHECK ((ai_retry_attempts >= 0)),
    CONSTRAINT ck_ai_threshold_positive CHECK ((ai_failure_threshold > 0)),
    CONSTRAINT ck_ai_timeout_positive CHECK ((ai_timeout_seconds > 0)),
    CONSTRAINT ck_dashboard_interval_positive CHECK ((dashboard_interval_seconds > 0)),
    CONSTRAINT ck_mcp_interval_positive CHECK ((mcp_interval_seconds > 0)),
    CONSTRAINT ck_mcp_recovery_positive CHECK ((mcp_recovery_check_interval > 0)),
    CONSTRAINT ck_mcp_retry_non_negative CHECK ((mcp_retry_attempts >= 0)),
    CONSTRAINT ck_mcp_threshold_positive CHECK ((mcp_failure_threshold > 0)),
    CONSTRAINT ck_mcp_timeout_positive CHECK ((mcp_timeout_seconds > 0))
);


--
-- Name: idp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.idp_servers (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    issuer character varying(500) NOT NULL,
    type character varying(50) NOT NULL,
    well_known_url character varying(500),
    authorization_endpoint character varying(500),
    token_endpoint character varying(500),
    revocation_endpoint character varying(500),
    introspection_endpoint character varying(500),
    jwks_uri character varying(500),
    userinfo_endpoint character varying(500),
    registration_endpoint character varying(500),
    supports_pkce boolean NOT NULL,
    supports_refresh_tokens boolean NOT NULL,
    supports_client_credentials boolean NOT NULL,
    token_endpoint_auth_methods json NOT NULL,
    scopes_supported json NOT NULL,
    response_types_supported json NOT NULL,
    grant_types_supported json NOT NULL,
    cached_config json,
    last_discovered timestamp with time zone,
    discovery_cache_ttl integer NOT NULL,
    enabled boolean NOT NULL,
    verified boolean NOT NULL,
    description text,
    logo_url character varying(500),
    protocol character varying(20),
    saml_metadata_url text,
    saml_metadata_xml text,
    saml_idp_entity_id text,
    saml_idp_sso_url text,
    saml_idp_certificate text,
    saml_sp_entity_id text,
    saml_attribute_mapping json,
    saml_group_attribute character varying(255),
    saml_group_to_role_mapping json,
    saml_force_authn boolean,
    saml_want_assertions_signed boolean,
    trust_saml_email_unverified boolean DEFAULT false NOT NULL,
    saml_last_metadata_refresh timestamp with time zone,
    workload_audiences json,
    target_user_type character varying(20),
    saml_group_to_end_user_group_mapping json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: iso42001_control_coverage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.iso42001_control_coverage (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    control_id character varying(16) NOT NULL,
    status character varying(32) DEFAULT 'not_implemented'::character varying NOT NULL,
    owner character varying(255),
    evidence jsonb,
    notes text,
    last_reviewed timestamp with time zone,
    next_review timestamp with time zone,
    updated_by character varying(255),
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: iso42001_controls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.iso42001_controls (
    control_id character varying(16) NOT NULL,
    clause character varying(16) NOT NULL,
    category character varying(255) NOT NULL,
    title character varying(255) NOT NULL,
    summary text,
    brutor_note text,
    brutor_capability character varying(16) DEFAULT 'org'::character varying NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL
);


--
-- Name: kb_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_chunks (
    id character varying(255) NOT NULL,
    document_id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    token_count integer,
    qdrant_point_id character varying(255),
    metadata_json json,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kb_collections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_collections (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(1024),
    enabled boolean,
    top_k integer,
    similarity_threshold double precision,
    max_context_tokens integer,
    chunk_size_tokens integer DEFAULT 500 NOT NULL,
    chunk_overlap_tokens integer DEFAULT 50 NOT NULL,
    system_prompt_template text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying NOT NULL
);


--
-- Name: kb_connector_checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_connector_checkpoints (
    connector_id character varying(255) NOT NULL,
    cursor json DEFAULT '{}'::json NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: kb_connector_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_connector_credentials (
    id character varying(255) NOT NULL,
    connector_id character varying(255) NOT NULL,
    credential_kind character varying(32) NOT NULL,
    encrypted_payload text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_kb_connector_credentials_kind CHECK (((credential_kind)::text = ANY ((ARRAY['api_key'::character varying, 'oauth'::character varying, 'service_account'::character varying])::text[])))
);


--
-- Name: kb_connector_identity_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_connector_identity_mappings (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    end_user_id integer NOT NULL,
    connector_type character varying(64) NOT NULL,
    source_principal character varying(512) NOT NULL,
    mapping_source character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    CONSTRAINT ck_kb_identity_connector_type CHECK (((connector_type)::text = ANY ((ARRAY['confluence'::character varying, 'notion'::character varying, 'google_drive'::character varying, 'slack'::character varying, 'github'::character varying, 'web_crawler'::character varying, 'jira'::character varying, 'sharepoint'::character varying])::text[]))),
    CONSTRAINT ck_kb_identity_mapping_source CHECK (((mapping_source)::text = ANY ((ARRAY['scim'::character varying, 'oidc_claim'::character varying, 'manual'::character varying, 'self_link'::character varying])::text[])))
);


--
-- Name: kb_connector_run_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_connector_run_logs (
    id character varying(255) NOT NULL,
    connector_id character varying(255) NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status character varying(32) NOT NULL,
    trigger character varying(32) DEFAULT 'schedule'::character varying NOT NULL,
    docs_seen integer DEFAULT 0 NOT NULL,
    docs_indexed integer DEFAULT 0 NOT NULL,
    docs_failed integer DEFAULT 0 NOT NULL,
    error_message text,
    CONSTRAINT ck_kb_connector_run_logs_status CHECK (((status)::text = ANY ((ARRAY['success'::character varying, 'failure'::character varying, 'running'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_kb_connector_run_logs_trigger CHECK (((trigger)::text = ANY ((ARRAY['schedule'::character varying, 'manual'::character varying, 'initial'::character varying])::text[])))
);


--
-- Name: kb_connectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_connectors (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(1024),
    connector_type character varying(64) NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    collection_id character varying(255),
    schedule_cron character varying(128),
    enabled boolean DEFAULT true NOT NULL,
    auto_derive_from_oidc boolean DEFAULT false NOT NULL,
    next_run_at timestamp with time zone,
    last_run_started_at timestamp with time zone,
    last_run_finished_at timestamp with time zone,
    last_run_status character varying(32),
    last_run_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255),
    CONSTRAINT ck_kb_connectors_status CHECK (((last_run_status IS NULL) OR ((last_run_status)::text = ANY ((ARRAY['success'::character varying, 'failure'::character varying, 'running'::character varying])::text[])))),
    CONSTRAINT ck_kb_connectors_type CHECK (((connector_type)::text = ANY ((ARRAY['confluence'::character varying, 'notion'::character varying, 'google_drive'::character varying, 'slack'::character varying, 'github'::character varying, 'web_crawler'::character varying, 'jira'::character varying, 'sharepoint'::character varying])::text[])))
);


--
-- Name: kb_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_documents (
    id character varying(255) NOT NULL,
    collection_id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(512) NOT NULL,
    filename character varying(512) NOT NULL,
    version character varying(50),
    author character varying(255),
    content_type character varying(100) NOT NULL,
    file_size_bytes integer,
    raw_content bytea,
    original_content text,
    status character varying(20) NOT NULL,
    error_message text,
    chunk_count integer DEFAULT 0 NOT NULL,
    embedding_model character varying(255),
    source character varying(32) DEFAULT 'upload'::character varying NOT NULL,
    source_url character varying(2048),
    source_updated_at timestamp with time zone,
    connector_doc_key character varying(512),
    acl jsonb,
    metadata_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    CONSTRAINT ck_kb_documents_source CHECK (((source)::text = ANY ((ARRAY['upload'::character varying, 'connector'::character varying])::text[])))
);


--
-- Name: kubernetes_clusters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kubernetes_clusters (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    context_name character varying(255) NOT NULL,
    api_server character varying(512),
    is_default boolean,
    created_at timestamp with time zone
);


--
-- Name: kubernetes_deployments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kubernetes_deployments (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    deployment_name character varying(255) NOT NULL,
    namespace character varying(255) NOT NULL,
    mcp_server_id character varying(255),
    mcp_server_name character varying(255),
    image_id integer,
    image_name character varying(512),
    deployment_uid character varying(255),
    pod_selector character varying(255),
    service_name character varying(255),
    replicas integer NOT NULL,
    container_port integer NOT NULL,
    service_port integer,
    service_type character varying(50) NOT NULL,
    cpu_request character varying(50),
    cpu_limit character varying(50),
    memory_request character varying(50),
    memory_limit character varying(50),
    github_repo character varying(512),
    deployed_by character varying(255),
    deployed_at timestamp with time zone NOT NULL,
    status character varying(50) NOT NULL,
    last_status_check timestamp with time zone,
    health_status character varying(50),
    ready_replicas integer,
    available_replicas integer,
    restart_count integer,
    last_restart_at timestamp with time zone
);


--
-- Name: llm_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_catalog (
    id character varying(255) NOT NULL,
    provider_key character varying(64) NOT NULL,
    model_name character varying(200) NOT NULL,
    display_name character varying(255) NOT NULL,
    description text,
    mode character varying(32) NOT NULL,
    capabilities jsonb,
    context_window integer,
    max_output_tokens integer,
    default_cost_per_input_token double precision,
    default_cost_per_output_token double precision,
    cost_per_image double precision,
    supported_image_sizes jsonb,
    supported_image_qualities jsonb,
    max_images_per_request integer,
    cost_per_audio_char double precision,
    supported_voices jsonb,
    supported_audio_formats jsonb,
    cost_per_audio_minute double precision,
    max_audio_duration_seconds integer,
    supported_input_formats jsonb,
    supported_response_formats jsonb,
    cost_per_video_second double precision,
    supported_video_resolutions jsonb,
    supported_video_durations jsonb,
    max_video_duration_seconds integer,
    thinking_enabled boolean,
    additional_drop_params jsonb,
    parameters double precision,
    tags jsonb,
    retire_date date,
    icon_url character varying(1024),
    recommended boolean DEFAULT false NOT NULL,
    deprecated boolean DEFAULT false NOT NULL,
    source character varying(16) DEFAULT 'builtin'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_model; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_model (
    id character varying(100) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    usage text,
    provider_id character varying(64) NOT NULL,
    model_name character varying(200) NOT NULL,
    mode character varying(20) NOT NULL,
    parameters double precision,
    tags json,
    api_url_auto_discovered boolean NOT NULL,
    api_url character varying(500),
    auth_type character varying(50),
    api_key text,
    provider_config text,
    default_temperature double precision,
    default_max_tokens integer,
    system_prompt text,
    top_p double precision,
    n integer,
    stop_sequences json,
    seed integer,
    presence_penalty double precision,
    frequency_penalty double precision,
    logit_bias json,
    response_format json,
    logprobs boolean,
    top_logprobs integer,
    stream_options json,
    supports_streaming boolean,
    supports_function_calling boolean,
    supports_vision boolean,
    supports_batch boolean,
    tools json,
    tool_choice character varying(50),
    parallel_tool_calls boolean,
    user_identifier character varying(255),
    api_version character varying(50),
    extra_headers json,
    fallback_models jsonb,
    model_metadata json,
    drop_params boolean DEFAULT true,
    additional_drop_params json,
    thinking_enabled boolean DEFAULT false NOT NULL,
    routing_weight integer,
    rate_limit_rpm integer,
    rate_limit_tpm integer,
    routing_group_id character varying(100),
    cost_per_input_token double precision,
    cost_per_output_token double precision,
    cost_per_image double precision,
    supported_image_sizes jsonb,
    supported_image_qualities jsonb,
    max_images_per_request integer DEFAULT 1 NOT NULL,
    cost_per_audio_char double precision,
    supported_voices json,
    supported_audio_formats json,
    cost_per_audio_minute double precision,
    max_audio_duration_seconds integer,
    supported_input_formats json,
    supported_response_formats jsonb,
    cost_per_video_second double precision,
    supported_video_resolutions json,
    supported_video_durations json,
    max_video_duration_seconds integer,
    timeout double precision,
    max_concurrent_requests integer,
    allowed_fails integer,
    cooldown_time integer,
    enabled boolean,
    tenant_limits json,
    tenant_governance json,
    retire_date date,
    retirement_announced_at date,
    retirement_reminder_date date,
    retirement_replacement_model_name character varying(255),
    retirement_replacement_provider_key character varying(100),
    retirement_notes text,
    capabilities json,
    catalog_id character varying(255),
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    source_external_id character varying(255),
    health_status character varying(50),
    last_health_check timestamp with time zone,
    last_health_success timestamp with time zone,
    consecutive_health_failures integer,
    health_check_error text,
    last_probe_type character varying(20),
    total_requests integer,
    failed_requests integer,
    total_tokens_consumed integer,
    avg_latency_ms double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying NOT NULL
);


--
-- Name: llm_provider_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_provider_catalog (
    id character varying(64) NOT NULL,
    key character varying(64) NOT NULL,
    display_name character varying(255) NOT NULL,
    category character varying(32),
    developer_url character varying(1024),
    api_key_url character varying(1024),
    pricing_url character varying(1024),
    model_catalog_url character varying(1024),
    icon_url character varying(1024),
    models_api_url character varying(1024),
    default_api_url character varying(1024),
    default_auth_type character varying(32),
    additional_drop_params json,
    region character varying(32),
    notes text,
    icon_data bytea,
    icon_mime_type character varying(100),
    icon_size integer,
    icon_hash character varying(64),
    source character varying(16) DEFAULT 'builtin'::character varying NOT NULL,
    supports_usage_import boolean DEFAULT false NOT NULL,
    import_api character varying(64),
    import_endpoint_tmpl character varying(512),
    import_capabilities json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_providers (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    key character varying(64) NOT NULL,
    display_name character varying(255) NOT NULL,
    category character varying(32),
    developer_url character varying(1024),
    api_key_url character varying(1024),
    pricing_url character varying(1024),
    model_catalog_url character varying(1024),
    icon_url character varying(1024),
    models_api_url character varying(1024),
    default_api_url character varying(1024),
    default_auth_type character varying(32),
    additional_drop_params json,
    notes text,
    metadata json,
    icon_data bytea,
    icon_mime_type character varying(100),
    icon_size integer,
    icon_hash character varying(64),
    icon_uploaded_at timestamp with time zone,
    enabled boolean DEFAULT true NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    region character varying(32),
    catalog_id character varying(64),
    import_enabled boolean DEFAULT false NOT NULL,
    import_credentials_encrypted text,
    import_endpoint_override character varying(512),
    import_scope json,
    import_mapping_policy json,
    import_schedule_cron character varying(128),
    import_next_run_at timestamp with time zone,
    import_backfill_through date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying NOT NULL
);


--
-- Name: llm_routing_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_routing_group (
    id character varying(100) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    routing_strategy character varying(50),
    complexity_config json,
    complexity_floor_tier character varying(20),
    mode character varying(20) DEFAULT 'chat'::character varying NOT NULL,
    cooldown_time integer,
    disable_cooldowns boolean,
    fallback_models json,
    context_window_fallbacks json,
    content_policy_fallbacks json,
    default_fallbacks json,
    required_capabilities json,
    num_retries integer,
    retry_after integer,
    stream_timeout double precision,
    retry_policy json,
    allowed_fails_policy json,
    max_fallbacks integer,
    routing_strategy_args json,
    alerting_enabled boolean,
    alerting_threshold integer,
    alerting_webhook_url character varying(500),
    alerting_config json,
    routing_max_retries integer,
    routing_retry_delay double precision,
    ttl_for_blocked_models integer,
    max_parallel_requests integer,
    rpm_limit integer,
    tpm_limit integer,
    router_settings json,
    enabled boolean,
    custom_metadata json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying NOT NULL
);


--
-- Name: llm_routing_group_tier; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_routing_group_tier (
    id character varying(36) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    routing_group_id character varying(36) NOT NULL,
    tier character varying(20) NOT NULL,
    llm_model_id character varying(36) NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: llm_runner_machines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_runner_machines (
    id character varying(100) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    host character varying(255) NOT NULL,
    port integer NOT NULL,
    adapter_type character varying(50) NOT NULL,
    kube_context character varying(255),
    kube_namespace character varying(255),
    hf_token_secret character varying(255),
    default_prefill_accelerator_type character varying(50),
    default_decode_accelerator_type character varying(50),
    prefill_nodepool_labels json,
    decode_nodepool_labels json,
    kv_cache_pool_config json,
    default_tensor_parallel integer,
    status character varying(20) NOT NULL,
    last_seen timestamp with time zone,
    last_error text,
    system_info json,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mcp_capability_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_capability_snapshots (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    mcp_server_id character varying(255) NOT NULL,
    snapshot_at timestamp with time zone DEFAULT now() NOT NULL,
    capabilities json NOT NULL,
    trigger character varying(32),
    content_hash character varying(64),
    is_change boolean DEFAULT false NOT NULL
);


--
-- Name: mcp_catalog_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_catalog_entries (
    id character varying(255) NOT NULL,
    tenant_id character varying(255),
    name character varying(200) NOT NULL,
    slug character varying(200) NOT NULL,
    short_description text,
    description text,
    icon_url character varying(500),
    icon_data bytea,
    icon_mime_type character varying(100),
    icon_size integer,
    icon_hash character varying(64),
    repo_url character varying(500),
    runtime character varying(20) NOT NULL,
    remote_config json,
    containerized_config json,
    env_vars json,
    tool_preview json,
    categories json,
    metadata_extra json,
    auth_pattern character varying(50) NOT NULL,
    source character varying(20) NOT NULL,
    enabled boolean NOT NULL,
    signature text,
    signature_algorithm character varying(50),
    signed_by character varying(50),
    signed_at timestamp with time zone,
    trust_tier character varying(20) DEFAULT 'unsigned'::character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: mcp_registry_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_registry_configs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    url character varying(500) NOT NULL,
    auth_type character varying(50) NOT NULL,
    auth_token text,
    description text,
    enabled boolean NOT NULL,
    verified boolean NOT NULL,
    timeout integer NOT NULL,
    is_default boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_sync timestamp with time zone
);


--
-- Name: mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_servers (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    base_url character varying(500) NOT NULL,
    mcp_endpoint_path character varying(200) NOT NULL,
    auth_type character varying(50),
    auth_token text,
    timeout double precision,
    max_concurrent_requests integer,
    retry_attempts integer,
    retry_after_ms integer,
    failure_threshold integer,
    recovery_timeout_secs integer,
    enabled boolean,
    mcp_apps_enabled boolean DEFAULT true NOT NULL,
    tenant_limits json,
    tenant_governance json,
    capabilities json,
    discovered_at timestamp with time zone,
    server_info json,
    protocol_version character varying(16),
    dialect character varying(16),
    wants_listener boolean DEFAULT true NOT NULL,
    health_status character varying(20),
    last_health_check timestamp with time zone,
    last_health_success timestamp with time zone,
    consecutive_health_failures integer,
    health_check_error text,
    icon_data bytea,
    icon_mime_type character varying(100),
    icon_size integer,
    icon_hash character varying(64),
    icon_uploaded_at timestamp with time zone,
    idp_id character varying(255),
    region character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying NOT NULL,
    CONSTRAINT ck_mcp_servers_max_concurrent_requests_positive CHECK (((max_concurrent_requests IS NULL) OR (max_concurrent_requests > 0)))
);


--
-- Name: media_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.media_files (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    kind character varying(40) NOT NULL,
    mime_type character varying(120) NOT NULL,
    size_bytes bigint NOT NULL,
    content_hash character varying(64),
    object_key character varying(1024) NOT NULL,
    storage_bucket_override character varying(255),
    chat_id character varying(64),
    model_id character varying(100),
    actor_id character varying(255),
    display_name character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone
);


--
-- Name: notification_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_channels (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    kind character varying(32) NOT NULL,
    config_encrypted text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_success_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    last_error text,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_nchan_kind CHECK (((kind)::text = ANY ((ARRAY['slack_webhook'::character varying, 'webhook'::character varying])::text[])))
);


--
-- Name: notification_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_deliveries (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    subscription_id character varying(64) NOT NULL,
    channel_id character varying(64) NOT NULL,
    inbox_event_id character varying(64) NOT NULL,
    inbox_item_id character varying(64) NOT NULL,
    fingerprint character varying(512) NOT NULL,
    status character varying(16) NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ndel_status CHECK (((status)::text = ANY ((ARRAY['sent'::character varying, 'failed'::character varying, 'coalesced'::character varying, 'skipped'::character varying, 'pending'::character varying])::text[])))
);


--
-- Name: notification_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_subscriptions (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    channel_id character varying(64) NOT NULL,
    event_class character varying(32) NOT NULL,
    severity_floor character varying(16) DEFAULT 'warning'::character varying NOT NULL,
    categories jsonb,
    ai_system_id character varying(255),
    coalesce_seconds integer DEFAULT 900 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_nsub_event_class CHECK (((event_class)::text = ANY ((ARRAY['inbox_item_created'::character varying, 'inbox_item_reoccurred'::character varying, 'digest_weekly'::character varying])::text[]))),
    CONSTRAINT ck_nsub_severity_floor CHECK (((severity_floor)::text = ANY ((ARRAY['info'::character varying, 'warning'::character varying, 'critical'::character varying])::text[])))
);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    user_id integer NOT NULL,
    notification_type character varying(50) NOT NULL,
    title character varying(500) NOT NULL,
    body text,
    metadata json,
    is_read boolean DEFAULT false NOT NULL,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone
);


--
-- Name: oauth_app_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_app_clients (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    app_type character varying(50) NOT NULL,
    idp_id character varying(255) NOT NULL,
    client_id character varying(500) NOT NULL,
    encrypted_client_secret text,
    redirect_uri text NOT NULL,
    scopes json NOT NULL,
    client_type character varying(50) NOT NULL,
    supports_pkce boolean NOT NULL,
    allowed_grant_types json NOT NULL,
    description text,
    registered_at timestamp with time zone DEFAULT now() NOT NULL,
    registered_by character varying(50),
    last_used timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    auto_registered boolean DEFAULT false NOT NULL,
    registration_client_uri character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: oauth_cimd_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_cimd_clients (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    idp_id character varying(255) NOT NULL,
    cimd_url character varying(2048) NOT NULL,
    idp_client_id character varying(512) NOT NULL,
    idp_client_secret_encrypted text,
    doc json,
    doc_fetched_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: oauth_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_tokens (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    gateway_user_id character varying(255),
    idp_user_id character varying(255) NOT NULL,
    mcp_server_id character varying(255) NOT NULL,
    idp_id character varying(255) NOT NULL,
    client_id character varying(500),
    encrypted_client_secret text,
    encrypted_access_token text NOT NULL,
    encrypted_refresh_token text,
    token_type character varying(50) NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    scopes text,
    issuer character varying(500),
    revoked boolean,
    revoked_at timestamp with time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    last_refreshed_at timestamp without time zone,
    refresh_count integer NOT NULL,
    last_actor_agent_id character varying(255),
    last_used_by_agent_at timestamp with time zone,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: oauth_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.oauth_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: oauth_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.oauth_tokens_id_seq OWNED BY public.oauth_tokens.id;


--
-- Name: observed_agent_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.observed_agent_events (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    event_id character varying(255) NOT NULL,
    collector_id character varying(255),
    device_id character varying(128),
    device_name character varying(255),
    client character varying(64) NOT NULL,
    session_id character varying(255),
    tool_name character varying(255) NOT NULL,
    tool_kind character varying(16) DEFAULT 'builtin'::character varying NOT NULL,
    mcp_server character varying(255),
    phase character varying(8) NOT NULL,
    decision character varying(16),
    occurred_at timestamp with time zone NOT NULL,
    duration_ms integer,
    exit_status integer,
    project character varying(255),
    source character varying(32) DEFAULT 'endpoint'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: platform_measurement_changes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_measurement_changes (
    id character varying(64) NOT NULL,
    metric_keys jsonb NOT NULL,
    bases jsonb NOT NULL,
    reason text NOT NULL,
    introduced_in_version character varying(32) NOT NULL,
    platform_version character varying(64),
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    epochs_written integer DEFAULT 0 NOT NULL
);


--
-- Name: platform_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_settings (
    id character varying(32) NOT NULL,
    global_key_allowed_target_tenants json,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(255)
);


--
-- Name: policy_bundle_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_bundle_events (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    bundle_id character varying(255),
    scope_group_id character varying(255) NOT NULL,
    event_type public.policyeventtype NOT NULL,
    diff json,
    dry_run_result json,
    admin_id character varying(255) NOT NULL,
    admin_type character varying(20) NOT NULL,
    ip_address character varying(50),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: policy_bundles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_bundles (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    scope_group_id character varying(255) NOT NULL,
    scope_group_path character varying(1000),
    name character varying(255) NOT NULL,
    description text,
    version character varying(50) NOT NULL,
    labels json,
    yaml_content text NOT NULL,
    resolved_config json,
    status public.policybundlestatus NOT NULL,
    is_active boolean NOT NULL,
    diff_summary json,
    source character varying(50),
    source_ref character varying(500),
    applied_at timestamp with time zone,
    applied_by character varying(255),
    rolled_back_at timestamp with time zone,
    rolled_back_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: policy_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policy_templates (
    id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    category character varying(100) NOT NULL,
    tags json,
    yaml_content text NOT NULL,
    is_system boolean NOT NULL,
    tenant_id character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: prompt_cache_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prompt_cache_policies (
    id character varying(50) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    strategy character varying(40) DEFAULT 'tools_and_system'::character varying NOT NULL,
    ttl_seconds integer DEFAULT 300 NOT NULL,
    min_request_tokens integer DEFAULT 1024 NOT NULL,
    sticky_routing boolean DEFAULT true NOT NULL,
    volatile_patterns json DEFAULT '[]'::json NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: proxy_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proxy_logs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    proxy_type character varying(20) NOT NULL,
    proxy_subtype character varying(50),
    skill_id character varying(255),
    skill_execution_id character varying(255),
    correlation_id character varying(255),
    parent_request_id character varying(255),
    request_id character varying(255),
    trace_root_kind character varying(32),
    trace_continuity character varying(32),
    source character varying(16) DEFAULT 'gateway'::character varying NOT NULL,
    record_id character varying(64),
    run_end_state character varying(24),
    outcome_id character varying(128),
    region_routed_to character varying(32),
    residency_class character varying(32),
    compliance_tags jsonb,
    guardrail_blocked boolean DEFAULT false NOT NULL,
    argument_policy_denied boolean DEFAULT false NOT NULL,
    pii_detected boolean DEFAULT false NOT NULL,
    is_auth_event boolean DEFAULT false NOT NULL,
    gateway_user_id character varying(255),
    gateway_user_username character varying(255),
    idp_user_id character varying(255),
    idp_user_username character varying(255),
    idp_user_email character varying(255),
    end_user_session_id character varying(255) NOT NULL,
    resource_group_id character varying(255),
    api_key_id character varying(255),
    api_key_name character varying(255),
    client_ip character varying(45),
    user_agent text,
    server_id character varying(255),
    idp_server_id character varying(255),
    http_method character varying(10) NOT NULL,
    endpoint text NOT NULL,
    target_url text,
    request_size_bytes integer,
    request_headers text,
    request_body_preview text,
    http_status_code integer,
    response_size_bytes integer,
    response_headers text,
    response_body_preview text,
    success boolean NOT NULL,
    error_message text,
    error_type character varying(100),
    duration_ms double precision,
    mcp_method character varying(100),
    mcp_request_id character varying(255),
    mcp_capability_name character varying(255),
    llm_model_requested character varying(200),
    llm_model_used character varying(200),
    llm_provider character varying(100),
    llm_prompt_tokens integer,
    llm_completion_tokens integer,
    llm_total_tokens integer,
    llm_streaming boolean,
    llm_temperature double precision,
    llm_max_tokens integer,
    llm_function_calling boolean,
    llm_used_fallback boolean,
    llm_fallback_reason character varying(100),
    llm_estimated_cost_usd double precision,
    media_images_generated integer,
    media_audio_chars bigint,
    media_audio_seconds_input double precision,
    media_video_seconds_output double precision,
    llm_cache_hit boolean,
    llm_cache_layer character varying(20),
    llm_tokens_saved integer,
    llm_cache_similarity_score double precision,
    prompt_cache_write_tokens integer,
    prompt_cache_read_tokens integer,
    prompt_cache_savings_usd numeric(12,6),
    llm_context_tokens integer,
    rag_enabled boolean,
    rag_chunks_retrieved integer,
    rag_context_tokens integer,
    rag_collection_ids character varying(1024),
    prompt_fingerprint character varying(64),
    rag_top_similarity_score double precision,
    rag_retrieval_duration_ms double precision,
    oauth_flow_type character varying(50),
    oauth_scope text,
    root_task_id character varying(64),
    delegation_depth integer,
    current_task_id character varying(64),
    parent_task_id character varying(64),
    a2a_protocol_version character varying(8),
    a2a_task_state character varying(32),
    agent_impl_name character varying(64),
    agent_impl_version character varying(64),
    agent_impl_build character varying(128),
    agent_impl_source character varying(16),
    client_label character varying(64),
    actor_agent_id character varying(255),
    subject_id character varying(255),
    subject_kind character varying(16),
    agent_authz_decision character varying(20),
    agent_grant_id character varying(255),
    semantic_policy_id character varying(255),
    step_id character varying(64),
    step_name character varying(128),
    turn_id character varying(64),
    turn_seq integer,
    argument_policy_id character varying(64),
    guardrail_config_id character varying(64),
    guardrail_check character varying(48),
    guardrail_action character varying(16),
    guardrails_degraded jsonb,
    guardrails_skipped jsonb,
    autonomy_level character varying(24),
    autonomy_provenance character varying(24),
    consumed_approval_request_id character varying(64),
    pre_action_state jsonb,
    chain_id character varying(64),
    chain_seq bigint,
    prev_hash character varying(64),
    row_hash character varying(64),
    chain_version character varying(8),
    bodies_pruned_at timestamp with time zone,
    erased_at timestamp with time zone,
    upstream_request_id character varying(128),
    request_content_hash character varying(64),
    response_content_hash character varying(64),
    trace_id character varying(32),
    span_id character varying(16),
    server_version character varying(64),
    tool_definition_hash character varying(64),
    deciding_model character varying(255),
    custom_metadata json,
    tags json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_proxy_logs_agent_impl_source CHECK (((agent_impl_source)::text = ANY ((ARRAY['token'::character varying, 'mcp_client_info'::character varying, 'baggage'::character varying, 'header'::character varying])::text[]))),
    CONSTRAINT ck_proxy_logs_chain_version CHECK (((chain_version)::text = ANY ((ARRAY['v1'::character varying, 'v2'::character varying, 'v3'::character varying, 'v4'::character varying])::text[])))
);


--
-- Name: proxy_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proxy_metrics (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    "timestamp" timestamp with time zone NOT NULL,
    proxy_type character varying(20) NOT NULL,
    source character varying(40) DEFAULT 'gateway'::character varying NOT NULL,
    request_id character varying(255) NOT NULL,
    correlation_id character varying(255),
    end_user_type character varying(20) NOT NULL,
    end_user_username character varying(255),
    gateway_user_id character varying(255),
    gateway_username character varying(255),
    client_ip character varying(45),
    user_agent_hash character varying(64),
    server_id character varying(255),
    virtual_server_id character varying(255),
    server_url text,
    http_method character varying(10) NOT NULL,
    endpoint text,
    endpoint_category character varying(50),
    total_duration_ms double precision NOT NULL,
    connect_time_ms double precision,
    handshake_time_ms double precision,
    processing_time_ms double precision,
    response_time_ms double precision,
    request_size_bytes integer,
    response_size_bytes integer,
    request_headers_count integer,
    response_headers_count integer,
    http_status_code integer,
    success boolean NOT NULL,
    error_type character varying(100),
    error_category character varying(50),
    retry_count integer,
    mcp_data json,
    llm_data json,
    oauth_data json,
    virtual_mcp_data json,
    skill_data json,
    dns_resolution_time_ms double precision,
    ssl_handshake_time_ms double precision,
    tcp_connect_time_ms double precision,
    server_response_headers text,
    cache_hit boolean,
    rate_limited boolean,
    authentication_time_ms double precision,
    memory_usage_mb double precision,
    cpu_usage_percent double precision,
    request_hour integer NOT NULL,
    request_day_of_week integer NOT NULL,
    timezone_offset integer
);


--
-- Name: resource_group_agent_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_agent_cards (
    id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    agent_card_id character varying(255) NOT NULL,
    limits json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: resource_group_agent_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_agent_members (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    agent_id character varying(255) NOT NULL,
    group_id character varying(255) NOT NULL,
    role character varying(50),
    added_by character varying(255),
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: resource_group_agent_members_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_group_agent_members_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: resource_group_agent_members_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_group_agent_members_id_seq OWNED BY public.resource_group_agent_members.id;


--
-- Name: resource_group_agent_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_agent_skills (
    id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    agent_skill_id character varying(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(255),
    limits json,
    governance json
);


--
-- Name: resource_group_argument_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_argument_policies (
    resource_group_id character varying(255) NOT NULL,
    argument_policy_id character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: resource_group_cache_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_cache_configs (
    resource_group_id character varying(255) NOT NULL,
    cache_config_id character varying(255) NOT NULL
);


--
-- Name: resource_group_deployment_machines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_deployment_machines (
    id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    machine_id character varying(100) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(255)
);


--
-- Name: resource_group_end_user_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_end_user_groups (
    resource_group_id character varying(255) NOT NULL,
    end_user_group_id character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: resource_group_guardrails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_guardrails (
    resource_group_id character varying(255) NOT NULL,
    guardrail_config_id character varying(255) NOT NULL
);


--
-- Name: resource_group_kb_collections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_kb_collections (
    resource_group_id character varying(255) NOT NULL,
    kb_collection_id character varying(255) NOT NULL
);


--
-- Name: resource_group_kb_connectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_kb_connectors (
    resource_group_id character varying(255) NOT NULL,
    kb_connector_id character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: resource_group_llm_governance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_llm_governance (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    llm_model_id character varying(255) NOT NULL,
    governance json NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    created_by character varying(255)
);


--
-- Name: resource_group_llm_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_llm_models (
    id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    llm_model_id character varying(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(255),
    limits json,
    portal_visible boolean DEFAULT true NOT NULL,
    auto_bound boolean DEFAULT false NOT NULL
);


--
-- Name: resource_group_mcp_capabilities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_mcp_capabilities (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    server_config_id character varying(255) NOT NULL,
    capability_type character varying(50) NOT NULL,
    capability_name character varying(500) NOT NULL,
    access character varying(20) NOT NULL,
    approval_timeout_seconds integer,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    created_by character varying(255)
);


--
-- Name: resource_group_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_memberships (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    user_id integer NOT NULL,
    group_id character varying(255) NOT NULL,
    role character varying(50),
    added_by character varying(50),
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: resource_group_memberships_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_group_memberships_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: resource_group_memberships_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_group_memberships_id_seq OWNED BY public.resource_group_memberships.id;


--
-- Name: resource_group_prompt_cache_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_prompt_cache_policies (
    resource_group_id character varying(50) NOT NULL,
    prompt_cache_policy_id character varying(50) NOT NULL,
    model_id character varying(50)
);


--
-- Name: resource_group_semantic_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_semantic_policies (
    resource_group_id character varying(255) NOT NULL,
    semantic_policy_id character varying(255) NOT NULL
);


--
-- Name: resource_group_server_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_group_server_configs (
    id character varying(255) NOT NULL,
    resource_group_id character varying(255) NOT NULL,
    server_config_id character varying(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    created_by character varying(255),
    limits json
);


--
-- Name: resource_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_groups (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    parent_group_id character varying(255),
    hierarchy_level integer NOT NULL,
    hierarchy_path character varying(1000),
    inherit_from_parent boolean NOT NULL,
    name character varying(255) NOT NULL,
    display_name character varying(255),
    description text,
    icon character varying(100),
    color character varying(50),
    group_type public.grouptype,
    system_kind character varying(24),
    baseline_opt_out boolean DEFAULT false NOT NULL,
    autonomy_level character varying(24),
    replay_suite_id character varying(64),
    operating_envelope json,
    declared_outcomes jsonb,
    run_idle_timeout_seconds integer,
    approval_window_seconds integer,
    owner character varying(255),
    intended_use text,
    intended_client_labels json,
    intended_client_systems json,
    enforce_accepted_callers boolean DEFAULT false NOT NULL,
    lifecycle_stage character varying(32),
    rollup_exclusion jsonb,
    successor_system_id character varying(255),
    no_successor_declared boolean,
    is_active boolean NOT NULL,
    is_system boolean NOT NULL,
    llm_global_limits json,
    mcp_global_limits json,
    llm_global_governance json,
    mcp_global_governance json,
    skill_global_governance json,
    skill_global_limits json,
    a2a_global_limits json,
    media_global_limits json,
    media_global_governance json,
    a2a_global_governance json,
    eu_ai_act_risk_tier character varying(32),
    eu_ai_act_role character varying(40),
    phi_resource boolean DEFAULT false NOT NULL,
    sensitive_data boolean DEFAULT false NOT NULL,
    source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    source_external_id character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying NOT NULL,
    CONSTRAINT ck_resource_groups_approval_window CHECK (((approval_window_seconds IS NULL) OR ((approval_window_seconds >= 60) AND (approval_window_seconds <= 604800))))
);


--
-- Name: router_cache_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.router_cache_configs (
    tenant_id character varying(64) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    ttl_secs integer DEFAULT 300 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT router_cache_configs_ttl_range CHECK (((ttl_secs >= 0) AND (ttl_secs <= 86400)))
);


--
-- Name: run_interventions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.run_interventions (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    root_task_id character varying(255) NOT NULL,
    action character varying(16) DEFAULT 'abort'::character varying NOT NULL,
    reason text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    first_blocked_at timestamp with time zone,
    steps_blocked integer DEFAULT 0 NOT NULL,
    status character varying(16) DEFAULT 'requested'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: saved_log_views; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_log_views (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    scope character varying(32) NOT NULL,
    owner_admin_id character varying(255),
    name character varying(255) NOT NULL,
    description text,
    filters jsonb NOT NULL,
    is_shared boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_saved_log_views_scope CHECK (((scope)::text = ANY ((ARRAY['proxy_logs'::character varying, 'admin_audit_logs'::character varying])::text[])))
);


--
-- Name: scheduler_runtime_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduler_runtime_state (
    scheduler_name character varying(64) NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    paused_at timestamp with time zone,
    paused_by character varying(255),
    pause_reason text,
    resumed_at timestamp with time zone,
    resumed_by character varying(255),
    last_beacon_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: schema_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_version (
    id integer NOT NULL,
    schema_version character varying(50) NOT NULL,
    app_version character varying(50) NOT NULL,
    schema_created_at timestamp with time zone DEFAULT now() NOT NULL,
    app_started_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scim_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scim_tokens (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    token_hash character varying(128) NOT NULL,
    token_prefix character varying(16) NOT NULL,
    idp_server_id character varying(255),
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(64)
);


--
-- Name: search_provider_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_provider_configs (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    provider character varying(50) NOT NULL,
    enabled boolean NOT NULL,
    api_key_encrypted text,
    google_cx_id character varying(255),
    searxng_base_url character varying(500),
    max_results_per_query integer NOT NULL,
    safe_search boolean NOT NULL,
    daily_query_limit integer,
    allowed_domains json,
    blocked_domains json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: search_provider_configs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.search_provider_configs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: search_provider_configs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.search_provider_configs_id_seq OWNED BY public.search_provider_configs.id;


--
-- Name: semantic_cache_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.semantic_cache_configs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(1024),
    enabled boolean,
    exact_match_enabled boolean,
    cache_key_mode character varying(20) DEFAULT 'last_user_turn'::character varying NOT NULL,
    exact_match_ttl_seconds integer,
    semantic_match_enabled boolean,
    similarity_threshold double precision,
    semantic_match_ttl_seconds integer,
    max_entries integer,
    time_bucketing character varying(10),
    exclude_tool_calls boolean,
    exclude_temperature_above double precision,
    exclude_guardrail_flagged boolean,
    shared_pool_enabled boolean,
    shared_pool_contribute boolean,
    shared_pool_consume boolean,
    shared_pool_model_filter json,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);


--
-- Name: semantic_cache_tool_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.semantic_cache_tool_rules (
    id integer NOT NULL,
    cache_config_id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    server_config_id character varying(255),
    capability_type character varying(20),
    capability_name character varying(500),
    cacheable boolean NOT NULL,
    priority integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: semantic_cache_tool_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.semantic_cache_tool_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: semantic_cache_tool_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.semantic_cache_tool_rules_id_seq OWNED BY public.semantic_cache_tool_rules.id;


--
-- Name: semantic_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.semantic_policies (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    constraint_text text NOT NULL,
    action character varying(16) DEFAULT 'block'::character varying NOT NULL,
    mode character varying(16) DEFAULT 'shadow'::character varying NOT NULL,
    surfaces jsonb,
    judge_model_id character varying(255),
    fail_open boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: server_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.server_configs (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    mcp_server_id character varying(255),
    virtual_server_id character varying(255),
    oauth_client_id character varying(255),
    name character varying(255) NOT NULL,
    description text,
    priority integer NOT NULL,
    is_default boolean NOT NULL,
    enabled boolean NOT NULL,
    display_name character varying(255),
    display_description text,
    icon_url character varying(512),
    usage_count integer NOT NULL,
    last_used timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: session_budget_resets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_budget_resets (
    tenant_id character varying(255) NOT NULL,
    session_id character varying(255) NOT NULL,
    reset_at timestamp with time zone DEFAULT now() NOT NULL,
    reset_by character varying(255),
    reason text
);


--
-- Name: skill_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skill_sources (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    repo_url character varying(1024) NOT NULL,
    git_ref character varying(255) DEFAULT 'main'::character varying NOT NULL,
    subpath character varying(512),
    enabled boolean DEFAULT true NOT NULL,
    sync_interval_seconds integer DEFAULT 3600 NOT NULL,
    auto_publish boolean DEFAULT false NOT NULL,
    status character varying(50) DEFAULT 'idle'::character varying NOT NULL,
    last_synced_at timestamp with time zone,
    last_attempted_at timestamp with time zone,
    last_commit_sha character varying(64),
    sync_error text,
    discovered_skill_count integer DEFAULT 0 NOT NULL,
    github_token_secret_ref character varying(512),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255)
);


--
-- Name: system_admin_audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_admin_audit_logs (
    id character varying(50) NOT NULL,
    admin_id character varying(50),
    action character varying(100) NOT NULL,
    resource_type character varying(50),
    resource_id character varying(100),
    affected_tenant_id character varying(255),
    ip_address character varying(45),
    user_agent text,
    request_method character varying(10),
    request_path character varying(500),
    success boolean NOT NULL,
    details json,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: system_admin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_admin_sessions (
    id character varying(50) NOT NULL,
    admin_id character varying(50) NOT NULL,
    token_hash character varying(255) NOT NULL,
    ip_address character varying(45),
    user_agent text,
    risk_score double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_activity timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean NOT NULL,
    deactivated_at timestamp with time zone,
    deactivation_reason character varying(100)
);


--
-- Name: system_admins; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_admins (
    id character varying(50) NOT NULL,
    username character varying(100) NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255) NOT NULL,
    full_name character varying(200),
    is_active boolean NOT NULL,
    is_locked boolean NOT NULL,
    failed_login_attempts integer NOT NULL,
    locked_until timestamp with time zone,
    permissions json NOT NULL,
    last_login timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(50),
    totp_secret text,
    totp_enabled boolean DEFAULT false NOT NULL,
    totp_verified_at timestamp with time zone,
    backup_codes json
);


--
-- Name: system_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_alerts (
    id character varying(64) NOT NULL,
    tenant_id character varying(255),
    severity character varying(16) NOT NULL,
    category character varying(32) NOT NULL,
    source character varying(255) NOT NULL,
    resource_type character varying(32),
    resource_id character varying(255),
    resource_name character varying(255),
    error_code character varying(64),
    title character varying(255) NOT NULL,
    message text NOT NULL,
    context jsonb,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    occurrence_count integer DEFAULT 1 NOT NULL,
    acknowledged_at timestamp with time zone,
    acknowledged_by character varying(255),
    resolved_at timestamp with time zone,
    resolved_by character varying(255),
    resolution_note text,
    dedup_key character varying(512) NOT NULL,
    CONSTRAINT ck_system_alerts_category CHECK (((category)::text = ANY ((ARRAY['downstream_provider'::character varying, 'downstream_idp'::character varying, 'downstream_cache'::character varying, 'downstream_vector_db'::character varying, 'resource_health'::character varying, 'infrastructure_threshold'::character varying, 'internal_error'::character varying, 'ai_system_liveness'::character varying, 'ai_system_behaviour'::character varying, 'ai_system_cost'::character varying, 'ai_system_dependency'::character varying])::text[]))),
    CONSTRAINT ck_system_alerts_severity CHECK (((severity)::text = ANY ((ARRAY['critical'::character varying, 'high'::character varying, 'medium'::character varying, 'low'::character varying])::text[]))),
    CONSTRAINT ck_system_alerts_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'acknowledged'::character varying, 'resolved'::character varying])::text[])))
);


--
-- Name: system_operation_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_operation_log (
    id character varying(64) NOT NULL,
    tenant_id character varying(255),
    actor_admin_id character varying(255) NOT NULL,
    actor_type character varying(32) NOT NULL,
    action character varying(64) NOT NULL,
    scope_kind character varying(32),
    scope_id character varying(255),
    payload jsonb,
    before_state jsonb,
    after_state jsonb,
    ip inet,
    user_agent text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_system_operation_log_actor_type CHECK (((actor_type)::text = ANY ((ARRAY['system_admin'::character varying, 'tenant_admin'::character varying, 'system'::character varying])::text[])))
);


--
-- Name: tenant_a2a_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_a2a_keys (
    tenant_id character varying(255) NOT NULL,
    public_key text NOT NULL,
    private_key_encrypted text NOT NULL,
    key_algorithm character varying(32) DEFAULT 'ed25519'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    rotated_at timestamp with time zone
);


--
-- Name: tenant_admin_audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_admin_audit_logs (
    id character varying(50) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    admin_id character varying(50),
    action character varying(100) NOT NULL,
    resource_type character varying(50),
    resource_id character varying(100),
    ip_address character varying(45),
    user_agent text,
    request_method character varying(10),
    request_path character varying(500),
    success boolean NOT NULL,
    details json,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tenant_admin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_admin_sessions (
    id character varying(50) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    admin_id character varying(50) NOT NULL,
    token_hash character varying(255) NOT NULL,
    ip_address character varying(45),
    user_agent text,
    risk_score double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_activity timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean NOT NULL,
    deactivated_at timestamp with time zone,
    deactivation_reason character varying(100)
);


--
-- Name: tenant_admins; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_admins (
    id character varying(50) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    role_id character varying(50) NOT NULL,
    username character varying(100) NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255) NOT NULL,
    full_name character varying(200),
    is_active boolean NOT NULL,
    is_locked boolean NOT NULL,
    failed_login_attempts integer NOT NULL,
    locked_until timestamp with time zone,
    last_login timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(50),
    avatar_url character varying(500),
    photo_data bytea,
    photo_mime_type character varying(100),
    photo_size integer,
    photo_hash character varying(64),
    totp_secret text,
    totp_enabled boolean NOT NULL,
    totp_verified_at timestamp with time zone,
    backup_codes json,
    password_change_required boolean DEFAULT false NOT NULL,
    idp_server_id character varying(255),
    idp_subject character varying(512),
    idp_last_login timestamp with time zone
);


--
-- Name: tenant_role_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_role_permissions (
    id character varying(50) NOT NULL,
    role_id character varying(50) NOT NULL,
    permission character varying(100) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tenant_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_roles (
    id character varying(50) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(100) NOT NULL,
    description text,
    is_system boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(50)
);


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenants (
    id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    display_name character varying(255) NOT NULL,
    organization_name character varying(500),
    domain character varying(255),
    primary_contact_email character varying(255) NOT NULL,
    primary_contact_name character varying(255),
    is_active boolean NOT NULL,
    is_default boolean NOT NULL,
    is_trial boolean NOT NULL,
    trial_ends_at timestamp with time zone,
    suspended boolean NOT NULL,
    suspended_at timestamp with time zone,
    suspension_reason text,
    max_admin_users integer,
    max_end_users integer,
    max_end_user_groups integer,
    max_llm_models integer,
    max_mcp_servers integer,
    max_mcp_registries integer,
    max_kb_collections integer,
    max_agent_skills integer,
    max_agent_cards integer,
    max_agent_identities integer,
    media_storage_quota_bytes bigint,
    default_embedding_model_id character varying(255),
    default_judge_model_id character varying(255),
    portal_google_drive_client_id character varying(255),
    portal_google_drive_api_key character varying(255),
    features json NOT NULL,
    settings json NOT NULL,
    ai_system_gate_policy json,
    assurance_capture_interval_hours integer,
    default_approval_window_seconds integer,
    mfa_policy character varying(20) DEFAULT 'optional'::character varying NOT NULL,
    billing_plan character varying(50),
    billing_email character varying(255),
    billing_metadata json,
    theme_primary_color character varying(7),
    theme_secondary_color character varying(7),
    theme_background_color character varying(7),
    theme_surface_color character varying(7),
    theme_text_primary character varying(7),
    theme_text_secondary character varying(7),
    theme_border_color character varying(7),
    theme_logo_url character varying(500),
    theme_favicon_url character varying(500),
    theme_company_name character varying(255),
    theme_variants jsonb,
    kb_oidc_auto_derive_enabled boolean DEFAULT true NOT NULL,
    maintenance_mode character varying(16) DEFAULT 'off'::character varying NOT NULL,
    maintenance_mode_at timestamp with time zone,
    maintenance_mode_by character varying(255),
    drain_grace_secs integer,
    data_residency jsonb,
    data_retention jsonb,
    compliance_profile jsonb DEFAULT '{"frameworks": []}'::jsonb NOT NULL,
    created_by character varying(50),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_active_at timestamp with time zone,
    CONSTRAINT ck_tenants_default_approval_window CHECK (((default_approval_window_seconds IS NULL) OR ((default_approval_window_seconds >= 60) AND (default_approval_window_seconds <= 604800))))
);


--
-- Name: tool_approval_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_approval_requests (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    server_config_id character varying(255),
    server_name character varying(500),
    capability_type character varying(50) NOT NULL,
    capability_name character varying(500) NOT NULL,
    requester_type character varying(20) NOT NULL,
    requester_user_id character varying(255),
    requester_api_key_id character varying(255),
    requester_group_id character varying(255) NOT NULL,
    requester_display character varying(500),
    call_arguments json,
    request_metadata json,
    status character varying(20) NOT NULL,
    approval_token character varying(128),
    token_used character varying(1) DEFAULT 'N'::character varying NOT NULL,
    timeout_seconds integer NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    resolved_by character varying(255),
    resolved_at timestamp with time zone,
    resolution_note text,
    originating_trace_root_id character varying(64),
    originating_parent_task_id character varying(64),
    originating_trace_root_kind character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: usage_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_alerts (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    resource_type character varying(20) NOT NULL,
    group_id character varying(255) NOT NULL,
    group_name character varying(255),
    alert_type character varying(20) NOT NULL,
    metric character varying(50) NOT NULL,
    threshold_pct integer NOT NULL,
    current_value bigint NOT NULL,
    current_value_float double precision,
    limit_value bigint NOT NULL,
    limit_value_float double precision,
    utilization_pct double precision NOT NULL,
    period character varying(20) NOT NULL,
    period_start timestamp with time zone NOT NULL,
    status character varying(20) NOT NULL,
    notified_at timestamp with time zone,
    acknowledged_by character varying(255),
    acknowledged_at timestamp with time zone,
    resolved_at timestamp with time zone,
    resolution_note text,
    triggered_by_request_id character varying(255),
    message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: usage_import_checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_import_checkpoints (
    provider_id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    last_imported_through timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: usage_import_run_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_import_run_logs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    provider_id character varying(64) NOT NULL,
    import_api character varying(64),
    trigger character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    rows_ingested integer DEFAULT 0 NOT NULL,
    users_created integer DEFAULT 0 NOT NULL,
    groups_created integer DEFAULT 0 NOT NULL,
    models_created integer DEFAULT 0 NOT NULL,
    memberships_created integer DEFAULT 0 NOT NULL,
    model_links_created integer DEFAULT 0 NOT NULL,
    cost_imported double precision DEFAULT '0'::double precision NOT NULL,
    currency character varying(10) DEFAULT 'USD'::character varying NOT NULL,
    error_message text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);


--
-- Name: usage_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_logs (
    id integer NOT NULL,
    tenant_id character varying(255) NOT NULL,
    user_id integer,
    group_id character varying(255),
    ancestry_path character varying(1000),
    group_name character varying(255),
    resource_type character varying(50) NOT NULL,
    resource_id character varying(255) NOT NULL,
    resource_name character varying(255),
    is_inherited boolean NOT NULL,
    inherited_from_group_id character varying(255),
    skill_id character varying(255),
    skill_execution_id character varying(255),
    prompt_tokens integer,
    completion_tokens integer,
    total_tokens integer,
    model_name character varying(255),
    provider character varying(50),
    mcp_tool_name character varying(255),
    mcp_execution_time_ms integer,
    mcp_call_count integer,
    mcp_data_size_bytes integer,
    estimated_cost double precision,
    currency character varying(10),
    prompt_cost double precision,
    completion_cost double precision,
    request_id character varying(255) NOT NULL,
    status_code integer,
    error_message text,
    response_time_ms integer,
    http_method character varying(10),
    endpoint character varying(255),
    client_ip character varying(45),
    user_agent character varying(255),
    source character varying(40) DEFAULT 'gateway'::character varying NOT NULL,
    ingest_grain character varying(16),
    import_run_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: usage_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.usage_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: usage_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.usage_logs_id_seq OWNED BY public.usage_logs.id;


--
-- Name: video_generation_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_generation_jobs (
    id character varying(64) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    model_id character varying(100),
    actor_id character varying(255),
    prompt text NOT NULL,
    resolution character varying(40),
    duration_seconds integer,
    chat_id character varying(64),
    status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    provider_request_id character varying(255),
    output_file_id character varying(64),
    error text,
    cost_usd double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: virtual_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.virtual_servers (
    id character varying(255) NOT NULL,
    tenant_id character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    capabilities json NOT NULL,
    owner character varying(50) NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: witnesses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.witnesses (
    id character varying(64) NOT NULL,
    scope character varying(16) NOT NULL,
    tenant_id character varying(255),
    kind character varying(16) NOT NULL,
    url text NOT NULL,
    operator_label character varying(255) NOT NULL,
    pinned_key text,
    enabled boolean DEFAULT true NOT NULL,
    same_operator boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by character varying(255),
    updated_by character varying(255)
);


--
-- Name: docker_deployments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments ALTER COLUMN id SET DEFAULT nextval('public.docker_deployments_id_seq'::regclass);


--
-- Name: docker_images id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_images ALTER COLUMN id SET DEFAULT nextval('public.docker_images_id_seq'::regclass);


--
-- Name: end_users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users ALTER COLUMN id SET DEFAULT nextval('public.end_users_id_seq'::regclass);


--
-- Name: oauth_tokens id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens ALTER COLUMN id SET DEFAULT nextval('public.oauth_tokens_id_seq'::regclass);


--
-- Name: resource_group_agent_members id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_members ALTER COLUMN id SET DEFAULT nextval('public.resource_group_agent_members_id_seq'::regclass);


--
-- Name: resource_group_memberships id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_memberships ALTER COLUMN id SET DEFAULT nextval('public.resource_group_memberships_id_seq'::regclass);


--
-- Name: search_provider_configs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_provider_configs ALTER COLUMN id SET DEFAULT nextval('public.search_provider_configs_id_seq'::regclass);


--
-- Name: semantic_cache_tool_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_tool_rules ALTER COLUMN id SET DEFAULT nextval('public.semantic_cache_tool_rules_id_seq'::regclass);


--
-- Name: usage_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs ALTER COLUMN id SET DEFAULT nextval('public.usage_logs_id_seq'::regclass);


--
-- Name: a2a_push_notification_configs a2a_push_notification_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_push_notification_configs
    ADD CONSTRAINT a2a_push_notification_configs_pkey PRIMARY KEY (id);


--
-- Name: a2a_tasks a2a_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_tasks
    ADD CONSTRAINT a2a_tasks_pkey PRIMARY KEY (id);


--
-- Name: action_records action_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.action_records
    ADD CONSTRAINT action_records_pkey PRIMARY KEY (record_id);


--
-- Name: admin_stream_tickets admin_stream_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_stream_tickets
    ADD CONSTRAINT admin_stream_tickets_pkey PRIMARY KEY (ticket_hash);


--
-- Name: agent_card_capabilities agent_card_capabilities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_card_capabilities
    ADD CONSTRAINT agent_card_capabilities_pkey PRIMARY KEY (id);


--
-- Name: agent_cards agent_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_cards
    ADD CONSTRAINT agent_cards_pkey PRIMARY KEY (id);


--
-- Name: agent_grants agent_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_grants
    ADD CONSTRAINT agent_grants_pkey PRIMARY KEY (id);


--
-- Name: agent_identities agent_identities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identities
    ADD CONSTRAINT agent_identities_pkey PRIMARY KEY (id);


--
-- Name: agent_identity_approval_history agent_identity_approval_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identity_approval_history
    ADD CONSTRAINT agent_identity_approval_history_pkey PRIMARY KEY (id);


--
-- Name: agent_implementation_releases agent_implementation_releases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_implementation_releases
    ADD CONSTRAINT agent_implementation_releases_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_assets agent_skill_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_assets
    ADD CONSTRAINT agent_skill_assets_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_dependencies agent_skill_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_dependencies
    ADD CONSTRAINT agent_skill_dependencies_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_executions agent_skill_executions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_executions
    ADD CONSTRAINT agent_skill_executions_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_resources agent_skill_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT agent_skill_resources_pkey PRIMARY KEY (id);


--
-- Name: agent_skill_scripts agent_skill_scripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_scripts
    ADD CONSTRAINT agent_skill_scripts_pkey PRIMARY KEY (id);


--
-- Name: agent_skills agent_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT agent_skills_pkey PRIMARY KEY (id);


--
-- Name: ai_asset_factsheets ai_asset_factsheets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_asset_factsheets
    ADD CONSTRAINT ai_asset_factsheets_pkey PRIMARY KEY (id);


--
-- Name: ai_assets ai_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_assets
    ADD CONSTRAINT ai_assets_pkey PRIMARY KEY (id);


--
-- Name: ai_system_assurance_reports ai_system_assurance_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_assurance_reports
    ADD CONSTRAINT ai_system_assurance_reports_pkey PRIMARY KEY (id);


--
-- Name: ai_system_baseline_epochs ai_system_baseline_epochs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baseline_epochs
    ADD CONSTRAINT ai_system_baseline_epochs_pkey PRIMARY KEY (id);


--
-- Name: ai_system_baselines ai_system_baselines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baselines
    ADD CONSTRAINT ai_system_baselines_pkey PRIMARY KEY (id);


--
-- Name: ai_system_compliance_profiles ai_system_compliance_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_compliance_profiles
    ADD CONSTRAINT ai_system_compliance_profiles_pkey PRIMARY KEY (ai_system_id);


--
-- Name: ai_system_contracts ai_system_contracts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_contracts
    ADD CONSTRAINT ai_system_contracts_pkey PRIMARY KEY (id);


--
-- Name: ai_system_data_scopes ai_system_data_scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_data_scopes
    ADD CONSTRAINT ai_system_data_scopes_pkey PRIMARY KEY (id);


--
-- Name: ai_system_dependencies ai_system_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT ai_system_dependencies_pkey PRIMARY KEY (id);


--
-- Name: ai_system_drift_events ai_system_drift_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_drift_events
    ADD CONSTRAINT ai_system_drift_events_pkey PRIMARY KEY (id);


--
-- Name: ai_system_evidence ai_system_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_evidence
    ADD CONSTRAINT ai_system_evidence_pkey PRIMARY KEY (id);


--
-- Name: ai_system_health ai_system_health_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_health
    ADD CONSTRAINT ai_system_health_pkey PRIMARY KEY (id);


--
-- Name: ai_system_incident_window_findings ai_system_incident_window_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_window_findings
    ADD CONSTRAINT ai_system_incident_window_findings_pkey PRIMARY KEY (incident_window_id, drift_event_id);


--
-- Name: ai_system_incident_windows ai_system_incident_windows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_windows
    ADD CONSTRAINT ai_system_incident_windows_pkey PRIMARY KEY (id);


--
-- Name: ai_system_lifecycle_events ai_system_lifecycle_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_lifecycle_events
    ADD CONSTRAINT ai_system_lifecycle_events_pkey PRIMARY KEY (id);


--
-- Name: ai_system_liveness_expectations ai_system_liveness_expectations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_liveness_expectations
    ADD CONSTRAINT ai_system_liveness_expectations_pkey PRIMARY KEY (id);


--
-- Name: ai_system_reach_rules ai_system_reach_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_reach_rules
    ADD CONSTRAINT ai_system_reach_rules_pkey PRIMARY KEY (id);


--
-- Name: ai_system_record_access ai_system_record_access_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_record_access
    ADD CONSTRAINT ai_system_record_access_pkey PRIMARY KEY (id);


--
-- Name: ai_system_replay_cases ai_system_replay_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_cases
    ADD CONSTRAINT ai_system_replay_cases_pkey PRIMARY KEY (id);


--
-- Name: ai_system_replay_runs ai_system_replay_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_runs
    ADD CONSTRAINT ai_system_replay_runs_pkey PRIMARY KEY (id);


--
-- Name: ai_system_replay_suites ai_system_replay_suites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_suites
    ADD CONSTRAINT ai_system_replay_suites_pkey PRIMARY KEY (id);


--
-- Name: ai_system_response_policies ai_system_response_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_response_policies
    ADD CONSTRAINT ai_system_response_policies_pkey PRIMARY KEY (id);


--
-- Name: ai_system_run_feedback ai_system_run_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_feedback
    ADD CONSTRAINT ai_system_run_feedback_pkey PRIMARY KEY (id);


--
-- Name: ai_system_run_scopes ai_system_run_scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_scopes
    ADD CONSTRAINT ai_system_run_scopes_pkey PRIMARY KEY (id);


--
-- Name: ai_system_run_segments ai_system_run_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT ai_system_run_segments_pkey PRIMARY KEY (id);


--
-- Name: ai_system_runs ai_system_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_runs
    ADD CONSTRAINT ai_system_runs_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: argument_policies argument_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.argument_policies
    ADD CONSTRAINT argument_policies_pkey PRIMARY KEY (id);


--
-- Name: assurance_check_results assurance_check_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_results
    ADD CONSTRAINT assurance_check_results_pkey PRIMARY KEY (id);


--
-- Name: assurance_check_reviews assurance_check_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_reviews
    ADD CONSTRAINT assurance_check_reviews_pkey PRIMARY KEY (id);


--
-- Name: assurance_check_rubrics assurance_check_rubrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_rubrics
    ADD CONSTRAINT assurance_check_rubrics_pkey PRIMARY KEY (id);


--
-- Name: assurance_check_versions assurance_check_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_versions
    ADD CONSTRAINT assurance_check_versions_pkey PRIMARY KEY (id);


--
-- Name: assurance_checks assurance_checks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_checks
    ADD CONSTRAINT assurance_checks_pkey PRIMARY KEY (id);


--
-- Name: assurance_digests assurance_digests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_digests
    ADD CONSTRAINT assurance_digests_pkey PRIMARY KEY (id);


--
-- Name: assurance_inbox_events assurance_inbox_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_inbox_events
    ADD CONSTRAINT assurance_inbox_events_pkey PRIMARY KEY (id);


--
-- Name: assurance_inbox_items assurance_inbox_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_inbox_items
    ADD CONSTRAINT assurance_inbox_items_pkey PRIMARY KEY (id);


--
-- Name: audit_chain_checkpoints audit_chain_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_chain_checkpoints
    ADD CONSTRAINT audit_chain_checkpoints_pkey PRIMARY KEY (id);


--
-- Name: audit_chain_prunes audit_chain_prunes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_chain_prunes
    ADD CONSTRAINT audit_chain_prunes_pkey PRIMARY KEY (id);


--
-- Name: audit_export_destinations audit_export_destinations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_export_destinations
    ADD CONSTRAINT audit_export_destinations_pkey PRIMARY KEY (id);


--
-- Name: batch_jobs batch_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_jobs
    ADD CONSTRAINT batch_jobs_pkey PRIMARY KEY (id);


--
-- Name: batch_requests batch_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_requests
    ADD CONSTRAINT batch_requests_pkey PRIMARY KEY (id);


--
-- Name: collector_keys collector_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_keys
    ADD CONSTRAINT collector_keys_pkey PRIMARY KEY (key_id);


--
-- Name: compliance_documentation_exports compliance_documentation_exports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_documentation_exports
    ADD CONSTRAINT compliance_documentation_exports_pkey PRIMARY KEY (id);


--
-- Name: compliance_frameworks compliance_frameworks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_frameworks
    ADD CONSTRAINT compliance_frameworks_pkey PRIMARY KEY (id);


--
-- Name: compliance_incident_events compliance_incident_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_incident_events
    ADD CONSTRAINT compliance_incident_events_pkey PRIMARY KEY (id);


--
-- Name: compliance_incidents compliance_incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_incidents
    ADD CONSTRAINT compliance_incidents_pkey PRIMARY KEY (id);


--
-- Name: compliance_notice_configs compliance_notice_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_notice_configs
    ADD CONSTRAINT compliance_notice_configs_pkey PRIMARY KEY (id);


--
-- Name: compliance_reports compliance_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_reports
    ADD CONSTRAINT compliance_reports_pkey PRIMARY KEY (id);


--
-- Name: data_erasure_events data_erasure_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_erasure_events
    ADD CONSTRAINT data_erasure_events_pkey PRIMARY KEY (id);


--
-- Name: disclosure_records disclosure_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.disclosure_records
    ADD CONSTRAINT disclosure_records_pkey PRIMARY KEY (id);


--
-- Name: discovered_assets discovered_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovered_assets
    ADD CONSTRAINT discovered_assets_pkey PRIMARY KEY (id);


--
-- Name: discovery_events discovery_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovery_events
    ADD CONSTRAINT discovery_events_pkey PRIMARY KEY (event_id, tenant_id);


--
-- Name: discovery_evidence discovery_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovery_evidence
    ADD CONSTRAINT discovery_evidence_pkey PRIMARY KEY (id);


--
-- Name: docker_deployments docker_deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT docker_deployments_pkey PRIMARY KEY (id);


--
-- Name: docker_images docker_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_images
    ADD CONSTRAINT docker_images_pkey PRIMARY KEY (id);


--
-- Name: ecs_deployments ecs_deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecs_deployments
    ADD CONSTRAINT ecs_deployments_pkey PRIMARY KEY (id);


--
-- Name: ecs_deployments ecs_deployments_task_arn_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecs_deployments
    ADD CONSTRAINT ecs_deployments_task_arn_key UNIQUE (task_arn);


--
-- Name: edition_marker edition_marker_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edition_marker
    ADD CONSTRAINT edition_marker_pkey PRIMARY KEY (id);


--
-- Name: end_user_group_members end_user_group_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_group_members
    ADD CONSTRAINT end_user_group_members_pkey PRIMARY KEY (end_user_group_id, end_user_id);


--
-- Name: end_user_groups end_user_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_groups
    ADD CONSTRAINT end_user_groups_pkey PRIMARY KEY (id);


--
-- Name: end_user_revoked_tokens end_user_revoked_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_revoked_tokens
    ADD CONSTRAINT end_user_revoked_tokens_pkey PRIMARY KEY (jti);


--
-- Name: end_users end_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT end_users_pkey PRIMARY KEY (id);


--
-- Name: evidence_bundles evidence_bundles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_bundles
    ADD CONSTRAINT evidence_bundles_pkey PRIMARY KEY (id);


--
-- Name: evidence_logs evidence_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_logs
    ADD CONSTRAINT evidence_logs_pkey PRIMARY KEY (log_id);


--
-- Name: evidence_logs evidence_logs_tenant_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_logs
    ADD CONSTRAINT evidence_logs_tenant_id_key UNIQUE (tenant_id);


--
-- Name: evidence_receipts evidence_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_receipts
    ADD CONSTRAINT evidence_receipts_pkey PRIMARY KEY (id);


--
-- Name: evidence_settings evidence_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_settings
    ADD CONSTRAINT evidence_settings_pkey PRIMARY KEY (tenant_id);


--
-- Name: evidence_signing_keys evidence_signing_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_signing_keys
    ADD CONSTRAINT evidence_signing_keys_pkey PRIMARY KEY (kid);


--
-- Name: evidence_tree_heads evidence_tree_heads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_tree_heads
    ADD CONSTRAINT evidence_tree_heads_pkey PRIMARY KEY (id);


--
-- Name: guardrail_break_glass guardrail_break_glass_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrail_break_glass
    ADD CONSTRAINT guardrail_break_glass_pkey PRIMARY KEY (id);


--
-- Name: guardrail_configs guardrail_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrail_configs
    ADD CONSTRAINT guardrail_configs_pkey PRIMARY KEY (id);


--
-- Name: health_check_configs health_check_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.health_check_configs
    ADD CONSTRAINT health_check_configs_pkey PRIMARY KEY (id);


--
-- Name: idp_servers idp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idp_servers
    ADD CONSTRAINT idp_servers_pkey PRIMARY KEY (id);


--
-- Name: iso42001_control_coverage iso42001_control_coverage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso42001_control_coverage
    ADD CONSTRAINT iso42001_control_coverage_pkey PRIMARY KEY (id);


--
-- Name: iso42001_controls iso42001_controls_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso42001_controls
    ADD CONSTRAINT iso42001_controls_pkey PRIMARY KEY (control_id);


--
-- Name: kb_chunks kb_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunks
    ADD CONSTRAINT kb_chunks_pkey PRIMARY KEY (id);


--
-- Name: kb_collections kb_collections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_collections
    ADD CONSTRAINT kb_collections_pkey PRIMARY KEY (id);


--
-- Name: kb_connector_checkpoints kb_connector_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_checkpoints
    ADD CONSTRAINT kb_connector_checkpoints_pkey PRIMARY KEY (connector_id);


--
-- Name: kb_connector_credentials kb_connector_credentials_connector_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_credentials
    ADD CONSTRAINT kb_connector_credentials_connector_id_key UNIQUE (connector_id);


--
-- Name: kb_connector_credentials kb_connector_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_credentials
    ADD CONSTRAINT kb_connector_credentials_pkey PRIMARY KEY (id);


--
-- Name: kb_connector_identity_mappings kb_connector_identity_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_identity_mappings
    ADD CONSTRAINT kb_connector_identity_mappings_pkey PRIMARY KEY (id);


--
-- Name: kb_connector_run_logs kb_connector_run_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_run_logs
    ADD CONSTRAINT kb_connector_run_logs_pkey PRIMARY KEY (id);


--
-- Name: kb_connectors kb_connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connectors
    ADD CONSTRAINT kb_connectors_pkey PRIMARY KEY (id);


--
-- Name: kb_documents kb_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_documents
    ADD CONSTRAINT kb_documents_pkey PRIMARY KEY (id);


--
-- Name: kubernetes_clusters kubernetes_clusters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kubernetes_clusters
    ADD CONSTRAINT kubernetes_clusters_pkey PRIMARY KEY (id);


--
-- Name: kubernetes_deployments kubernetes_deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kubernetes_deployments
    ADD CONSTRAINT kubernetes_deployments_pkey PRIMARY KEY (id);


--
-- Name: llm_catalog llm_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_catalog
    ADD CONSTRAINT llm_catalog_pkey PRIMARY KEY (id);


--
-- Name: llm_model llm_model_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_model
    ADD CONSTRAINT llm_model_pkey PRIMARY KEY (id);


--
-- Name: llm_provider_catalog llm_provider_catalog_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_catalog
    ADD CONSTRAINT llm_provider_catalog_key_key UNIQUE (key);


--
-- Name: llm_provider_catalog llm_provider_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_catalog
    ADD CONSTRAINT llm_provider_catalog_pkey PRIMARY KEY (id);


--
-- Name: llm_providers llm_providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_providers
    ADD CONSTRAINT llm_providers_pkey PRIMARY KEY (id);


--
-- Name: llm_routing_group llm_routing_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_routing_group
    ADD CONSTRAINT llm_routing_group_pkey PRIMARY KEY (id);


--
-- Name: llm_routing_group_tier llm_routing_group_tier_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_routing_group_tier
    ADD CONSTRAINT llm_routing_group_tier_pkey PRIMARY KEY (id);


--
-- Name: llm_runner_machines llm_runner_machines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_runner_machines
    ADD CONSTRAINT llm_runner_machines_pkey PRIMARY KEY (id);


--
-- Name: mcp_capability_snapshots mcp_capability_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_capability_snapshots
    ADD CONSTRAINT mcp_capability_snapshots_pkey PRIMARY KEY (id);


--
-- Name: mcp_catalog_entries mcp_catalog_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_catalog_entries
    ADD CONSTRAINT mcp_catalog_entries_pkey PRIMARY KEY (id);


--
-- Name: mcp_registry_configs mcp_registry_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_registry_configs
    ADD CONSTRAINT mcp_registry_configs_pkey PRIMARY KEY (id);


--
-- Name: mcp_servers mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id);


--
-- Name: media_files media_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_pkey PRIMARY KEY (id);


--
-- Name: notification_channels notification_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_channels
    ADD CONSTRAINT notification_channels_pkey PRIMARY KEY (id);


--
-- Name: notification_deliveries notification_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_pkey PRIMARY KEY (id);


--
-- Name: notification_subscriptions notification_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_subscriptions
    ADD CONSTRAINT notification_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: oauth_app_clients oauth_app_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_app_clients
    ADD CONSTRAINT oauth_app_clients_pkey PRIMARY KEY (id);


--
-- Name: oauth_cimd_clients oauth_cimd_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_cimd_clients
    ADD CONSTRAINT oauth_cimd_clients_pkey PRIMARY KEY (id);


--
-- Name: oauth_tokens oauth_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_pkey PRIMARY KEY (id);


--
-- Name: observed_agent_events observed_agent_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observed_agent_events
    ADD CONSTRAINT observed_agent_events_pkey PRIMARY KEY (id);


--
-- Name: evidence_log_entries pk_evidence_log_entries; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_log_entries
    ADD CONSTRAINT pk_evidence_log_entries PRIMARY KEY (log_id, seq);


--
-- Name: evidence_log_nodes pk_evidence_log_nodes; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_log_nodes
    ADD CONSTRAINT pk_evidence_log_nodes PRIMARY KEY (log_id, level, index);


--
-- Name: platform_measurement_changes platform_measurement_changes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_measurement_changes
    ADD CONSTRAINT platform_measurement_changes_pkey PRIMARY KEY (id);


--
-- Name: platform_settings platform_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_settings
    ADD CONSTRAINT platform_settings_pkey PRIMARY KEY (id);


--
-- Name: policy_bundle_events policy_bundle_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_bundle_events
    ADD CONSTRAINT policy_bundle_events_pkey PRIMARY KEY (id);


--
-- Name: policy_bundles policy_bundles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_bundles
    ADD CONSTRAINT policy_bundles_pkey PRIMARY KEY (id);


--
-- Name: policy_templates policy_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_templates
    ADD CONSTRAINT policy_templates_pkey PRIMARY KEY (id);


--
-- Name: prompt_cache_policies prompt_cache_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_cache_policies
    ADD CONSTRAINT prompt_cache_policies_pkey PRIMARY KEY (id);


--
-- Name: proxy_logs proxy_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_logs
    ADD CONSTRAINT proxy_logs_pkey PRIMARY KEY (id);


--
-- Name: proxy_metrics proxy_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_metrics
    ADD CONSTRAINT proxy_metrics_pkey PRIMARY KEY (id);


--
-- Name: resource_group_agent_cards resource_group_agent_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_cards
    ADD CONSTRAINT resource_group_agent_cards_pkey PRIMARY KEY (id);


--
-- Name: resource_group_agent_members resource_group_agent_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_members
    ADD CONSTRAINT resource_group_agent_members_pkey PRIMARY KEY (id);


--
-- Name: resource_group_agent_skills resource_group_agent_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_skills
    ADD CONSTRAINT resource_group_agent_skills_pkey PRIMARY KEY (id);


--
-- Name: resource_group_argument_policies resource_group_argument_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_argument_policies
    ADD CONSTRAINT resource_group_argument_policies_pkey PRIMARY KEY (resource_group_id, argument_policy_id);


--
-- Name: resource_group_cache_configs resource_group_cache_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_cache_configs
    ADD CONSTRAINT resource_group_cache_configs_pkey PRIMARY KEY (resource_group_id, cache_config_id);


--
-- Name: resource_group_deployment_machines resource_group_deployment_machines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_deployment_machines
    ADD CONSTRAINT resource_group_deployment_machines_pkey PRIMARY KEY (id);


--
-- Name: resource_group_end_user_groups resource_group_end_user_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_end_user_groups
    ADD CONSTRAINT resource_group_end_user_groups_pkey PRIMARY KEY (resource_group_id, end_user_group_id);


--
-- Name: resource_group_guardrails resource_group_guardrails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_guardrails
    ADD CONSTRAINT resource_group_guardrails_pkey PRIMARY KEY (resource_group_id, guardrail_config_id);


--
-- Name: resource_group_kb_collections resource_group_kb_collections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_kb_collections
    ADD CONSTRAINT resource_group_kb_collections_pkey PRIMARY KEY (resource_group_id, kb_collection_id);


--
-- Name: resource_group_kb_connectors resource_group_kb_connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_kb_connectors
    ADD CONSTRAINT resource_group_kb_connectors_pkey PRIMARY KEY (resource_group_id, kb_connector_id);


--
-- Name: resource_group_llm_governance resource_group_llm_governance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_governance
    ADD CONSTRAINT resource_group_llm_governance_pkey PRIMARY KEY (id);


--
-- Name: resource_group_llm_models resource_group_llm_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_models
    ADD CONSTRAINT resource_group_llm_models_pkey PRIMARY KEY (id);


--
-- Name: resource_group_mcp_capabilities resource_group_mcp_capabilities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_mcp_capabilities
    ADD CONSTRAINT resource_group_mcp_capabilities_pkey PRIMARY KEY (id);


--
-- Name: resource_group_memberships resource_group_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_memberships
    ADD CONSTRAINT resource_group_memberships_pkey PRIMARY KEY (id);


--
-- Name: resource_group_prompt_cache_policies resource_group_prompt_cache_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_prompt_cache_policies
    ADD CONSTRAINT resource_group_prompt_cache_policies_pkey PRIMARY KEY (resource_group_id, prompt_cache_policy_id);


--
-- Name: resource_group_semantic_policies resource_group_semantic_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_semantic_policies
    ADD CONSTRAINT resource_group_semantic_policies_pkey PRIMARY KEY (resource_group_id, semantic_policy_id);


--
-- Name: resource_group_server_configs resource_group_server_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_server_configs
    ADD CONSTRAINT resource_group_server_configs_pkey PRIMARY KEY (id);


--
-- Name: resource_groups resource_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_groups
    ADD CONSTRAINT resource_groups_pkey PRIMARY KEY (id);


--
-- Name: router_cache_configs router_cache_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_cache_configs
    ADD CONSTRAINT router_cache_configs_pkey PRIMARY KEY (tenant_id);


--
-- Name: run_interventions run_interventions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_interventions
    ADD CONSTRAINT run_interventions_pkey PRIMARY KEY (id);


--
-- Name: saved_log_views saved_log_views_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_log_views
    ADD CONSTRAINT saved_log_views_pkey PRIMARY KEY (id);


--
-- Name: scheduler_runtime_state scheduler_runtime_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduler_runtime_state
    ADD CONSTRAINT scheduler_runtime_state_pkey PRIMARY KEY (scheduler_name);


--
-- Name: schema_version schema_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_version
    ADD CONSTRAINT schema_version_pkey PRIMARY KEY (id);


--
-- Name: scim_tokens scim_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scim_tokens
    ADD CONSTRAINT scim_tokens_pkey PRIMARY KEY (id);


--
-- Name: search_provider_configs search_provider_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_provider_configs
    ADD CONSTRAINT search_provider_configs_pkey PRIMARY KEY (id);


--
-- Name: search_provider_configs search_provider_configs_tenant_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_provider_configs
    ADD CONSTRAINT search_provider_configs_tenant_id_key UNIQUE (tenant_id);


--
-- Name: semantic_cache_configs semantic_cache_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_configs
    ADD CONSTRAINT semantic_cache_configs_pkey PRIMARY KEY (id);


--
-- Name: semantic_cache_tool_rules semantic_cache_tool_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_tool_rules
    ADD CONSTRAINT semantic_cache_tool_rules_pkey PRIMARY KEY (id);


--
-- Name: semantic_policies semantic_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_policies
    ADD CONSTRAINT semantic_policies_pkey PRIMARY KEY (id);


--
-- Name: server_configs server_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_configs
    ADD CONSTRAINT server_configs_pkey PRIMARY KEY (id);


--
-- Name: session_budget_resets session_budget_resets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_budget_resets
    ADD CONSTRAINT session_budget_resets_pkey PRIMARY KEY (tenant_id, session_id);


--
-- Name: skill_sources skill_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skill_sources
    ADD CONSTRAINT skill_sources_pkey PRIMARY KEY (id);


--
-- Name: system_admin_audit_logs system_admin_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admin_audit_logs
    ADD CONSTRAINT system_admin_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: system_admin_sessions system_admin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admin_sessions
    ADD CONSTRAINT system_admin_sessions_pkey PRIMARY KEY (id);


--
-- Name: system_admins system_admins_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admins
    ADD CONSTRAINT system_admins_pkey PRIMARY KEY (id);


--
-- Name: system_alerts system_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_alerts
    ADD CONSTRAINT system_alerts_pkey PRIMARY KEY (id);


--
-- Name: system_operation_log system_operation_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_operation_log
    ADD CONSTRAINT system_operation_log_pkey PRIMARY KEY (id);


--
-- Name: tenant_a2a_keys tenant_a2a_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_a2a_keys
    ADD CONSTRAINT tenant_a2a_keys_pkey PRIMARY KEY (tenant_id);


--
-- Name: tenant_admin_audit_logs tenant_admin_audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admin_audit_logs
    ADD CONSTRAINT tenant_admin_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: tenant_admin_sessions tenant_admin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admin_sessions
    ADD CONSTRAINT tenant_admin_sessions_pkey PRIMARY KEY (id);


--
-- Name: tenant_admins tenant_admins_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admins
    ADD CONSTRAINT tenant_admins_pkey PRIMARY KEY (id);


--
-- Name: tenant_role_permissions tenant_role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_role_permissions
    ADD CONSTRAINT tenant_role_permissions_pkey PRIMARY KEY (id);


--
-- Name: tenant_roles tenant_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT tenant_roles_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tool_approval_requests tool_approval_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_approval_requests
    ADD CONSTRAINT tool_approval_requests_pkey PRIMARY KEY (id);


--
-- Name: agent_card_capabilities uix_agent_card_caps_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_card_capabilities
    ADD CONSTRAINT uix_agent_card_caps_name UNIQUE (agent_card_id, capability_name);


--
-- Name: agent_cards uix_agent_cards_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_cards
    ADD CONSTRAINT uix_agent_cards_tenant_name UNIQUE (tenant_id, name);


--
-- Name: agent_identities uix_agent_identities_idp_subject; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identities
    ADD CONSTRAINT uix_agent_identities_idp_subject UNIQUE (idp_server_id, idp_subject);


--
-- Name: agent_identities uix_agent_identities_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identities
    ADD CONSTRAINT uix_agent_identities_tenant_name UNIQUE (tenant_id, name);


--
-- Name: agent_skill_assets uix_agent_skill_assets_skill_filename; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_assets
    ADD CONSTRAINT uix_agent_skill_assets_skill_filename UNIQUE (skill_id, filename);


--
-- Name: agent_skill_dependencies uix_agent_skill_deps_tenant_parent_dependency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_dependencies
    ADD CONSTRAINT uix_agent_skill_deps_tenant_parent_dependency UNIQUE (tenant_id, parent_skill_id, dependency_skill_id);


--
-- Name: agent_skill_resources uix_agent_skill_resources_skill_filename; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT uix_agent_skill_resources_skill_filename UNIQUE (skill_id, filename);


--
-- Name: agent_skill_scripts uix_agent_skill_scripts_skill_filename; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_scripts
    ADD CONSTRAINT uix_agent_skill_scripts_skill_filename UNIQUE (skill_id, filename);


--
-- Name: agent_skills uix_agent_skills_tenant_name_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT uix_agent_skills_tenant_name_version UNIQUE (tenant_id, name, version);


--
-- Name: ai_assets uix_ai_assets_tenant_kind_ref; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_assets
    ADD CONSTRAINT uix_ai_assets_tenant_kind_ref UNIQUE (tenant_id, asset_kind, ref_id);


--
-- Name: audit_export_destinations uix_audit_export_dest_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_export_destinations
    ADD CONSTRAINT uix_audit_export_dest_tenant_name UNIQUE (tenant_id, name);


--
-- Name: semantic_cache_tool_rules uix_cache_tool_rule; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_tool_rules
    ADD CONSTRAINT uix_cache_tool_rule UNIQUE (cache_config_id, server_config_id, capability_type, capability_name);


--
-- Name: discovered_assets uix_discovered_assets_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovered_assets
    ADD CONSTRAINT uix_discovered_assets_tenant_key UNIQUE (tenant_id, asset_key);


--
-- Name: docker_deployments uix_docker_deployments_tenant_container_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT uix_docker_deployments_tenant_container_id UNIQUE (tenant_id, container_id);


--
-- Name: docker_deployments uix_docker_deployments_tenant_container_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT uix_docker_deployments_tenant_container_name UNIQUE (tenant_id, container_name);


--
-- Name: docker_deployments uix_docker_deployments_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT uix_docker_deployments_tenant_name UNIQUE (tenant_id, deployment_name);


--
-- Name: docker_images uix_docker_images_tenant_image_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_images
    ADD CONSTRAINT uix_docker_images_tenant_image_id UNIQUE (tenant_id, image_id);


--
-- Name: resource_groups uix_end_user_groups_tenant_parent_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_groups
    ADD CONSTRAINT uix_end_user_groups_tenant_parent_name UNIQUE (tenant_id, parent_group_id, name);


--
-- Name: end_users uix_end_users_tenant_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT uix_end_users_tenant_email UNIQUE (tenant_id, email);


--
-- Name: end_users uix_end_users_tenant_username; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT uix_end_users_tenant_username UNIQUE (tenant_id, username);


--
-- Name: iso42001_control_coverage uix_iso42001_coverage_tenant_control; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso42001_control_coverage
    ADD CONSTRAINT uix_iso42001_coverage_tenant_control UNIQUE (tenant_id, control_id);


--
-- Name: llm_catalog uix_llm_catalog_provider_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_catalog
    ADD CONSTRAINT uix_llm_catalog_provider_model UNIQUE (provider_key, model_name);


--
-- Name: oauth_app_clients uix_oauth_app_clients_tenant_idp_client; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_app_clients
    ADD CONSTRAINT uix_oauth_app_clients_tenant_idp_client UNIQUE (tenant_id, idp_id, client_id);


--
-- Name: observed_agent_events uix_observed_agent_events_tenant_event; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observed_agent_events
    ADD CONSTRAINT uix_observed_agent_events_tenant_event UNIQUE (tenant_id, event_id);


--
-- Name: resource_group_memberships uix_resource_group_memberships_tenant_resouce_group; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_memberships
    ADD CONSTRAINT uix_resource_group_memberships_tenant_resouce_group UNIQUE (tenant_id, user_id, group_id);


--
-- Name: resource_group_agent_members uix_rg_agent_member; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_members
    ADD CONSTRAINT uix_rg_agent_member UNIQUE (tenant_id, agent_id, group_id);


--
-- Name: saved_log_views uix_saved_log_views_tenant_scope_owner_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_log_views
    ADD CONSTRAINT uix_saved_log_views_tenant_scope_owner_name UNIQUE (tenant_id, scope, owner_admin_id, name);


--
-- Name: system_admins uix_system_admins_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admins
    ADD CONSTRAINT uix_system_admins_email UNIQUE (email);


--
-- Name: system_admins uix_system_admins_username; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admins
    ADD CONSTRAINT uix_system_admins_username UNIQUE (username);


--
-- Name: tenant_admins uix_tenant_admins_tenant_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admins
    ADD CONSTRAINT uix_tenant_admins_tenant_email UNIQUE (tenant_id, email);


--
-- Name: tenant_admins uix_tenant_admins_tenant_username; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admins
    ADD CONSTRAINT uix_tenant_admins_tenant_username UNIQUE (tenant_id, username);


--
-- Name: tenant_role_permissions uix_tenant_role_permissions_role_permission; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_role_permissions
    ADD CONSTRAINT uix_tenant_role_permissions_role_permission UNIQUE (role_id, permission);


--
-- Name: tenant_roles uix_tenant_roles_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT uix_tenant_roles_tenant_name UNIQUE (tenant_id, name);


--
-- Name: tenants uix_tenants_domain; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT uix_tenants_domain UNIQUE (domain);


--
-- Name: tenants uix_tenants_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT uix_tenants_name UNIQUE (name);


--
-- Name: agent_implementation_releases uq_agent_implementation_releases_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_implementation_releases
    ADD CONSTRAINT uq_agent_implementation_releases_key UNIQUE (tenant_id, agent_id, name, version, build);


--
-- Name: ai_system_baseline_epochs uq_ai_system_baseline_epochs_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baseline_epochs
    ADD CONSTRAINT uq_ai_system_baseline_epochs_key UNIQUE (tenant_id, ai_system_id, metric_key, basis, caller_system_id, bucket, starts_at);


--
-- Name: ai_system_baselines uq_ai_system_baselines_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baselines
    ADD CONSTRAINT uq_ai_system_baselines_key UNIQUE (tenant_id, ai_system_id, metric_key, bucket, actor_id, basis, caller_system_id);


--
-- Name: ai_system_contracts uq_ai_system_contracts_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_contracts
    ADD CONSTRAINT uq_ai_system_contracts_version UNIQUE (tenant_id, ai_system_id, version);


--
-- Name: ai_system_data_scopes uq_ai_system_data_scopes_system; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_data_scopes
    ADD CONSTRAINT uq_ai_system_data_scopes_system UNIQUE (tenant_id, ai_system_id);


--
-- Name: ai_system_dependencies uq_ai_system_dependencies_system_card; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT uq_ai_system_dependencies_system_card UNIQUE (tenant_id, ai_system_id, agent_card_id);


--
-- Name: ai_system_dependencies uq_ai_system_dependencies_system_target; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT uq_ai_system_dependencies_system_target UNIQUE (tenant_id, ai_system_id, depends_on_system_id);


--
-- Name: ai_system_health uq_ai_system_health_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_health
    ADD CONSTRAINT uq_ai_system_health_key UNIQUE (tenant_id, ai_system_id);


--
-- Name: ai_system_liveness_expectations uq_ai_system_liveness_system; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_liveness_expectations
    ADD CONSTRAINT uq_ai_system_liveness_system UNIQUE (tenant_id, ai_system_id);


--
-- Name: ai_system_run_feedback uq_ai_system_run_feedback_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_feedback
    ADD CONSTRAINT uq_ai_system_run_feedback_user UNIQUE (tenant_id, root_task_id, user_id);


--
-- Name: ai_system_run_scopes uq_ai_system_run_scopes_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_scopes
    ADD CONSTRAINT uq_ai_system_run_scopes_key UNIQUE (tenant_id, root_task_id, scope_key);


--
-- Name: ai_system_run_segments uq_ai_system_run_segments_run_system; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT uq_ai_system_run_segments_run_system UNIQUE (tenant_id, run_id, segment_system_id);


--
-- Name: ai_system_runs uq_ai_system_runs_root; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_runs
    ADD CONSTRAINT uq_ai_system_runs_root UNIQUE (tenant_id, root_task_id);


--
-- Name: argument_policies uq_argument_policies_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.argument_policies
    ADD CONSTRAINT uq_argument_policies_tenant_name UNIQUE (tenant_id, name);


--
-- Name: batch_requests uq_batch_req_job_custom_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_requests
    ADD CONSTRAINT uq_batch_req_job_custom_id UNIQUE (batch_job_id, custom_id);


--
-- Name: end_user_groups uq_end_user_groups_tenant_idp_external; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_groups
    ADD CONSTRAINT uq_end_user_groups_tenant_idp_external UNIQUE (tenant_id, idp_server_id, source_external_id);


--
-- Name: end_user_groups uq_end_user_groups_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_groups
    ADD CONSTRAINT uq_end_user_groups_tenant_name UNIQUE (tenant_id, name);


--
-- Name: resource_group_agent_cards uq_group_agent_card; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_cards
    ADD CONSTRAINT uq_group_agent_card UNIQUE (resource_group_id, agent_card_id);


--
-- Name: resource_group_agent_skills uq_group_agent_skill; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_skills
    ADD CONSTRAINT uq_group_agent_skill UNIQUE (resource_group_id, agent_skill_id);


--
-- Name: resource_group_deployment_machines uq_group_deployment_machine; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_deployment_machines
    ADD CONSTRAINT uq_group_deployment_machine UNIQUE (resource_group_id, machine_id);


--
-- Name: resource_group_llm_governance uq_group_llm_governance; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_governance
    ADD CONSTRAINT uq_group_llm_governance UNIQUE (resource_group_id, llm_model_id);


--
-- Name: resource_group_llm_models uq_group_llm_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_models
    ADD CONSTRAINT uq_group_llm_model UNIQUE (resource_group_id, llm_model_id);


--
-- Name: resource_group_mcp_capabilities uq_group_server_capability; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_mcp_capabilities
    ADD CONSTRAINT uq_group_server_capability UNIQUE (resource_group_id, server_config_id, capability_type, capability_name);


--
-- Name: resource_group_server_configs uq_group_server_config; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_server_configs
    ADD CONSTRAINT uq_group_server_config UNIQUE (resource_group_id, server_config_id);


--
-- Name: kb_connectors uq_kb_connectors_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connectors
    ADD CONSTRAINT uq_kb_connectors_tenant_name UNIQUE (tenant_id, name);


--
-- Name: kb_connector_identity_mappings uq_kb_identity_user_type_principal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_identity_mappings
    ADD CONSTRAINT uq_kb_identity_user_type_principal UNIQUE (end_user_id, connector_type, source_principal);


--
-- Name: llm_providers uq_llm_providers_tenant_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_providers
    ADD CONSTRAINT uq_llm_providers_tenant_key UNIQUE (tenant_id, key);


--
-- Name: mcp_catalog_entries uq_mcp_catalog_tenant_slug; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_catalog_entries
    ADD CONSTRAINT uq_mcp_catalog_tenant_slug UNIQUE (tenant_id, slug);


--
-- Name: oauth_cimd_clients uq_oauth_cimd_clients_scope; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_cimd_clients
    ADD CONSTRAINT uq_oauth_cimd_clients_scope UNIQUE (tenant_id, idp_id, cimd_url);


--
-- Name: oauth_tokens uq_oauth_tokens_tenant_idp_user_server; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT uq_oauth_tokens_tenant_idp_user_server UNIQUE (tenant_id, idp_id, idp_user_id, mcp_server_id);


--
-- Name: policy_templates uq_policy_template_name_tenant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_templates
    ADD CONSTRAINT uq_policy_template_name_tenant UNIQUE (name, tenant_id);


--
-- Name: prompt_cache_policies uq_prompt_cache_policies_tenant_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_cache_policies
    ADD CONSTRAINT uq_prompt_cache_policies_tenant_name UNIQUE (tenant_id, name);


--
-- Name: ai_system_replay_suites uq_replay_suites_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_suites
    ADD CONSTRAINT uq_replay_suites_name UNIQUE (tenant_id, ai_system_id, name);


--
-- Name: ai_system_response_policies uq_response_policies_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_response_policies
    ADD CONSTRAINT uq_response_policies_name UNIQUE (tenant_id, ai_system_id, name);


--
-- Name: run_interventions uq_run_interventions_run; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_interventions
    ADD CONSTRAINT uq_run_interventions_run UNIQUE (tenant_id, root_task_id);


--
-- Name: server_configs uq_server_config_tenant_server_client_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_configs
    ADD CONSTRAINT uq_server_config_tenant_server_client_name UNIQUE (tenant_id, mcp_server_id, oauth_client_id, name);


--
-- Name: skill_sources uq_skill_source_tenant_repo_ref; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skill_sources
    ADD CONSTRAINT uq_skill_source_tenant_repo_ref UNIQUE (tenant_id, repo_url, git_ref);


--
-- Name: usage_alerts uq_usage_alert_per_period; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_alerts
    ADD CONSTRAINT uq_usage_alert_per_period UNIQUE (tenant_id, resource_type, group_id, metric, threshold_pct, period_start);


--
-- Name: usage_alerts usage_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_alerts
    ADD CONSTRAINT usage_alerts_pkey PRIMARY KEY (id);


--
-- Name: usage_import_checkpoints usage_import_checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_import_checkpoints
    ADD CONSTRAINT usage_import_checkpoints_pkey PRIMARY KEY (provider_id);


--
-- Name: usage_import_run_logs usage_import_run_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_import_run_logs
    ADD CONSTRAINT usage_import_run_logs_pkey PRIMARY KEY (id);


--
-- Name: usage_logs usage_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_pkey PRIMARY KEY (id);


--
-- Name: usage_logs usage_logs_request_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_request_id_key UNIQUE (request_id);


--
-- Name: video_generation_jobs video_generation_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_generation_jobs
    ADD CONSTRAINT video_generation_jobs_pkey PRIMARY KEY (id);


--
-- Name: virtual_servers virtual_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.virtual_servers
    ADD CONSTRAINT virtual_servers_pkey PRIMARY KEY (id);


--
-- Name: witnesses witnesses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.witnesses
    ADD CONSTRAINT witnesses_pkey PRIMARY KEY (id);


--
-- Name: ix_a2a_push_configs_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_push_configs_task ON public.a2a_push_notification_configs USING btree (task_id);


--
-- Name: ix_a2a_push_configs_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_push_configs_tenant ON public.a2a_push_notification_configs USING btree (tenant_id);


--
-- Name: ix_a2a_tasks_tenant_context; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_tasks_tenant_context ON public.a2a_tasks USING btree (tenant_id, context_id, status_timestamp);


--
-- Name: ix_a2a_tasks_tenant_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_tasks_tenant_state ON public.a2a_tasks USING btree (tenant_id, state, status_timestamp);


--
-- Name: ix_a2a_tasks_tenant_status_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_tasks_tenant_status_ts ON public.a2a_tasks USING btree (tenant_id, status_timestamp);


--
-- Name: ix_action_records_chain_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_records_chain_parent ON public.action_records USING btree (chain_parent_record_id) INCLUDE (record_id, chain_relation, tenant_id, "timestamp") WHERE (chain_parent_record_id IS NOT NULL);


--
-- Name: ix_action_records_human_disposed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_records_human_disposed ON public.action_records USING btree (tenant_id, "timestamp") WHERE human_disposed;


--
-- Name: ix_action_records_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_records_run ON public.action_records USING btree (tenant_id, root_task_id);


--
-- Name: ix_action_records_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_records_system ON public.action_records USING btree (tenant_id, ai_system_id, "timestamp");


--
-- Name: ix_action_records_tenant_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_records_tenant_ts ON public.action_records USING btree (tenant_id, "timestamp");


--
-- Name: ix_action_records_verdict; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_action_records_verdict ON public.action_records USING btree (tenant_id, verdict);


--
-- Name: ix_admin_stream_tickets_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_admin_stream_tickets_expires_at ON public.admin_stream_tickets USING btree (expires_at);


--
-- Name: ix_agent_card_caps_card; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_card_caps_card ON public.agent_card_capabilities USING btree (agent_card_id);


--
-- Name: ix_agent_card_caps_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_card_caps_name ON public.agent_card_capabilities USING btree (capability_name);


--
-- Name: ix_agent_cards_extensions_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_cards_extensions_gin ON public.agent_cards USING gin (extensions);


--
-- Name: ix_agent_cards_health_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_cards_health_status ON public.agent_cards USING btree (tenant_id, health_status);


--
-- Name: ix_agent_cards_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_cards_tenant ON public.agent_cards USING btree (tenant_id);


--
-- Name: ix_agent_cards_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_cards_tenant_enabled ON public.agent_cards USING btree (tenant_id, enabled);


--
-- Name: ix_agent_cards_trust_tier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_cards_trust_tier ON public.agent_cards USING btree (tenant_id, trust_tier);


--
-- Name: ix_agent_grants_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_grants_agent ON public.agent_grants USING btree (agent_id);


--
-- Name: ix_agent_grants_tenant_agent_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_grants_tenant_agent_action ON public.agent_grants USING btree (tenant_id, agent_id, action_type);


--
-- Name: ix_agent_identities_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_identities_tenant ON public.agent_identities USING btree (tenant_id);


--
-- Name: ix_agent_identities_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_identities_tenant_status ON public.agent_identities USING btree (tenant_id, status);


--
-- Name: ix_agent_identity_approval_history_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_identity_approval_history_agent ON public.agent_identity_approval_history USING btree (tenant_id, agent_id, valid_from);


--
-- Name: ix_agent_implementation_releases_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_implementation_releases_last_seen ON public.agent_implementation_releases USING btree (tenant_id, agent_id, last_seen_at);


--
-- Name: ix_agent_skill_assets_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_assets_hash ON public.agent_skill_assets USING btree (content_hash);


--
-- Name: ix_agent_skill_assets_skill_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_assets_skill_type ON public.agent_skill_assets USING btree (skill_id, asset_type);


--
-- Name: ix_agent_skill_assets_tenant_skill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_assets_tenant_skill ON public.agent_skill_assets USING btree (tenant_id, skill_id);


--
-- Name: ix_agent_skill_deps_parent_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_deps_parent_order ON public.agent_skill_dependencies USING btree (parent_skill_id, execution_order);


--
-- Name: ix_agent_skill_deps_tenant_dependency; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_deps_tenant_dependency ON public.agent_skill_dependencies USING btree (tenant_id, dependency_skill_id);


--
-- Name: ix_agent_skill_deps_tenant_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_deps_tenant_parent ON public.agent_skill_dependencies USING btree (tenant_id, parent_skill_id);


--
-- Name: ix_agent_skill_resources_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_resources_hash ON public.agent_skill_resources USING btree (content_hash);


--
-- Name: ix_agent_skill_resources_skill_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_resources_skill_type ON public.agent_skill_resources USING btree (skill_id, resource_type);


--
-- Name: ix_agent_skill_resources_tenant_skill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_resources_tenant_skill ON public.agent_skill_resources USING btree (tenant_id, skill_id);


--
-- Name: ix_agent_skill_scripts_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_scripts_hash ON public.agent_skill_scripts USING btree (script_hash);


--
-- Name: ix_agent_skill_scripts_tenant_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_scripts_tenant_mode ON public.agent_skill_scripts USING btree (tenant_id, execution_mode);


--
-- Name: ix_agent_skill_scripts_tenant_skill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skill_scripts_tenant_skill ON public.agent_skill_scripts USING btree (tenant_id, skill_id);


--
-- Name: ix_agent_skills_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_hash ON public.agent_skills USING btree (skill_md_hash);


--
-- Name: ix_agent_skills_source_repo_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_source_repo_id ON public.agent_skills USING btree (source_repo_id);


--
-- Name: ix_agent_skills_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_tenant_created ON public.agent_skills USING btree (tenant_id, created_at);


--
-- Name: ix_agent_skills_tenant_enabled_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_tenant_enabled_status ON public.agent_skills USING btree (tenant_id, enabled, status);


--
-- Name: ix_agent_skills_tenant_last_executed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_tenant_last_executed ON public.agent_skills USING btree (tenant_id, last_executed_at);


--
-- Name: ix_agent_skills_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_tenant_name ON public.agent_skills USING btree (tenant_id, name);


--
-- Name: ix_agent_skills_tenant_name_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_skills_tenant_name_status ON public.agent_skills USING btree (tenant_id, name, status);


--
-- Name: ix_ai_asset_factsheets_tenant_asset_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_asset_factsheets_tenant_asset_created ON public.ai_asset_factsheets USING btree (tenant_id, asset_id, created_at);


--
-- Name: ix_ai_assets_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_assets_tenant ON public.ai_assets USING btree (tenant_id);


--
-- Name: ix_ai_assets_tenant_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_assets_tenant_kind ON public.ai_assets USING btree (tenant_id, asset_kind);


--
-- Name: ix_ai_assets_tenant_lifecycle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_assets_tenant_lifecycle ON public.ai_assets USING btree (tenant_id, lifecycle_stage);


--
-- Name: ix_ai_assets_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_assets_tenant_status ON public.ai_assets USING btree (tenant_id, status);


--
-- Name: ix_ai_sys_compliance_profiles_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_sys_compliance_profiles_tenant ON public.ai_system_compliance_profiles USING btree (tenant_id);


--
-- Name: ix_ai_system_assurance_reports_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_assurance_reports_ai_system_id ON public.ai_system_assurance_reports USING btree (ai_system_id);


--
-- Name: ix_ai_system_assurance_reports_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_assurance_reports_tenant_id ON public.ai_system_assurance_reports USING btree (tenant_id);


--
-- Name: ix_ai_system_baselines_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_baselines_system ON public.ai_system_baselines USING btree (tenant_id, ai_system_id, state);


--
-- Name: ix_ai_system_contracts_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_contracts_ai_system_id ON public.ai_system_contracts USING btree (ai_system_id);


--
-- Name: ix_ai_system_contracts_system_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_contracts_system_status ON public.ai_system_contracts USING btree (tenant_id, ai_system_id, status);


--
-- Name: ix_ai_system_contracts_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_contracts_tenant_id ON public.ai_system_contracts USING btree (tenant_id);


--
-- Name: ix_ai_system_dependencies_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_dependencies_target ON public.ai_system_dependencies USING btree (tenant_id, depends_on_system_id);


--
-- Name: ix_ai_system_evidence_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_evidence_ai_system_id ON public.ai_system_evidence USING btree (ai_system_id);


--
-- Name: ix_ai_system_evidence_dependency; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_evidence_dependency ON public.ai_system_evidence USING btree (dependency_id);


--
-- Name: ix_ai_system_evidence_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_evidence_kind ON public.ai_system_evidence USING btree (tenant_id, ai_system_id, kind);


--
-- Name: ix_ai_system_evidence_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_evidence_system ON public.ai_system_evidence USING btree (tenant_id, ai_system_id);


--
-- Name: ix_ai_system_evidence_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_evidence_tenant_id ON public.ai_system_evidence USING btree (tenant_id);


--
-- Name: ix_ai_system_health_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_health_ai_system_id ON public.ai_system_health USING btree (ai_system_id);


--
-- Name: ix_ai_system_health_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_health_state ON public.ai_system_health USING btree (tenant_id, health_state);


--
-- Name: ix_ai_system_health_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_health_tenant_id ON public.ai_system_health USING btree (tenant_id);


--
-- Name: ix_ai_system_incident_window_findings_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_incident_window_findings_event ON public.ai_system_incident_window_findings USING btree (drift_event_id);


--
-- Name: ix_ai_system_incident_windows_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_incident_windows_scope ON public.ai_system_incident_windows USING btree (tenant_id, ai_system_id, starts_at);


--
-- Name: ix_ai_system_lifecycle_events_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_lifecycle_events_system ON public.ai_system_lifecycle_events USING btree (tenant_id, ai_system_id, created_at);


--
-- Name: ix_ai_system_liveness_tenant_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_liveness_tenant_mode ON public.ai_system_liveness_expectations USING btree (tenant_id, mode, enabled);


--
-- Name: ix_ai_system_reach_rules_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_reach_rules_system ON public.ai_system_reach_rules USING btree (tenant_id, ai_system_id);


--
-- Name: ix_ai_system_replay_cases_suite_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_cases_suite_id ON public.ai_system_replay_cases USING btree (suite_id);


--
-- Name: ix_ai_system_replay_cases_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_cases_tenant_id ON public.ai_system_replay_cases USING btree (tenant_id);


--
-- Name: ix_ai_system_replay_runs_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_runs_ai_system_id ON public.ai_system_replay_runs USING btree (ai_system_id);


--
-- Name: ix_ai_system_replay_runs_suite_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_runs_suite_id ON public.ai_system_replay_runs USING btree (suite_id);


--
-- Name: ix_ai_system_replay_runs_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_runs_tenant_id ON public.ai_system_replay_runs USING btree (tenant_id);


--
-- Name: ix_ai_system_replay_suites_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_suites_ai_system_id ON public.ai_system_replay_suites USING btree (ai_system_id);


--
-- Name: ix_ai_system_replay_suites_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_replay_suites_tenant_id ON public.ai_system_replay_suites USING btree (tenant_id);


--
-- Name: ix_ai_system_response_policies_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_response_policies_ai_system_id ON public.ai_system_response_policies USING btree (ai_system_id);


--
-- Name: ix_ai_system_response_policies_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_response_policies_tenant_id ON public.ai_system_response_policies USING btree (tenant_id);


--
-- Name: ix_ai_system_run_feedback_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_feedback_run ON public.ai_system_run_feedback USING btree (tenant_id, root_task_id);


--
-- Name: ix_ai_system_run_feedback_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_feedback_tenant_id ON public.ai_system_run_feedback USING btree (tenant_id);


--
-- Name: ix_ai_system_run_feedback_triage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_feedback_triage ON public.ai_system_run_feedback USING btree (tenant_id, score, created_at);


--
-- Name: ix_ai_system_run_scopes_declared; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_scopes_declared ON public.ai_system_run_scopes USING btree (declared_at);


--
-- Name: ix_ai_system_run_segments_root_first; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_segments_root_first ON public.ai_system_run_segments USING btree (tenant_id, root_system_id, first_action_at);


--
-- Name: ix_ai_system_run_segments_system_caller; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_segments_system_caller ON public.ai_system_run_segments USING btree (tenant_id, segment_system_id, caller_system_id);


--
-- Name: ix_ai_system_run_segments_system_first; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_run_segments_system_first ON public.ai_system_run_segments USING btree (tenant_id, segment_system_id, first_action_at);


--
-- Name: ix_ai_system_runs_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_actor ON public.ai_system_runs USING btree (tenant_id, actor_type, actor_id);


--
-- Name: ix_ai_system_runs_contract; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_contract ON public.ai_system_runs USING btree (tenant_id, contract_hash);


--
-- Name: ix_ai_system_runs_group_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_group_started ON public.ai_system_runs USING btree (tenant_id, resource_group_id, started_at);


--
-- Name: ix_ai_system_runs_system_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_system_started ON public.ai_system_runs USING btree (tenant_id, ai_system_id, started_at);


--
-- Name: ix_ai_system_runs_tenant_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_tenant_source ON public.ai_system_runs USING btree (tenant_id, source);


--
-- Name: ix_ai_system_runs_terminal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_terminal ON public.ai_system_runs USING btree (tenant_id, terminal_state, started_at);


--
-- Name: ix_ai_system_runs_tool_sig; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_system_runs_tool_sig ON public.ai_system_runs USING btree (tenant_id, ai_system_id, tool_signature);


--
-- Name: ix_api_keys_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_agent_id ON public.api_keys USING btree (agent_id);


--
-- Name: ix_api_keys_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_group_id ON public.api_keys USING btree (group_id);


--
-- Name: ix_api_keys_key_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_key_hash ON public.api_keys USING btree (key_hash);


--
-- Name: ix_api_keys_key_prefix; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_key_prefix ON public.api_keys USING btree (key_prefix);


--
-- Name: ix_api_keys_last_used; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_last_used ON public.api_keys USING btree (last_used_at);


--
-- Name: ix_api_keys_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_enabled ON public.api_keys USING btree (tenant_id, enabled);


--
-- Name: ix_api_keys_tenant_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_expires ON public.api_keys USING btree (tenant_id, expires_at);


--
-- Name: ix_api_keys_tenant_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_group ON public.api_keys USING btree (tenant_id, group_id);


--
-- Name: ix_api_keys_tenant_group_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_group_enabled ON public.api_keys USING btree (tenant_id, group_id, enabled);


--
-- Name: ix_api_keys_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_id ON public.api_keys USING btree (tenant_id);


--
-- Name: ix_api_keys_tenant_revoked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_revoked ON public.api_keys USING btree (tenant_id, revoked);


--
-- Name: ix_api_keys_tenant_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_scope ON public.api_keys USING btree (tenant_id, scope);


--
-- Name: ix_api_keys_tenant_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_tenant_user ON public.api_keys USING btree (tenant_id, user_id);


--
-- Name: ix_api_keys_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_user_id ON public.api_keys USING btree (user_id);


--
-- Name: ix_argument_policies_tenant_surface; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_argument_policies_tenant_surface ON public.argument_policies USING btree (tenant_id, surface);


--
-- Name: ix_assurance_check_results_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_check_results_tenant_id ON public.assurance_check_results USING btree (tenant_id);


--
-- Name: ix_assurance_check_reviews_queue; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_check_reviews_queue ON public.assurance_check_reviews USING btree (tenant_id, status, week);


--
-- Name: ix_assurance_check_rubrics_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_check_rubrics_tenant ON public.assurance_check_rubrics USING btree (tenant_id);


--
-- Name: ix_assurance_check_versions_check_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_check_versions_check_id ON public.assurance_check_versions USING btree (check_id);


--
-- Name: ix_assurance_check_versions_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_check_versions_tenant_id ON public.assurance_check_versions USING btree (tenant_id);


--
-- Name: ix_assurance_checks_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_checks_tenant_id ON public.assurance_checks USING btree (tenant_id);


--
-- Name: ix_assurance_digests_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_digests_tenant_id ON public.assurance_digests USING btree (tenant_id);


--
-- Name: ix_assurance_inbox_events_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_inbox_events_item_id ON public.assurance_inbox_events USING btree (item_id);


--
-- Name: ix_assurance_inbox_events_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_inbox_events_tenant_id ON public.assurance_inbox_events USING btree (tenant_id);


--
-- Name: ix_assurance_inbox_items_ai_system_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_inbox_items_ai_system_id ON public.assurance_inbox_items USING btree (ai_system_id);


--
-- Name: ix_assurance_inbox_items_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_inbox_items_tenant_id ON public.assurance_inbox_items USING btree (tenant_id);


--
-- Name: ix_assurance_reports_system_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assurance_reports_system_created ON public.ai_system_assurance_reports USING btree (tenant_id, ai_system_id, created_at);


--
-- Name: ix_audit_chain_checkpoints_chain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_chain_checkpoints_chain ON public.audit_chain_checkpoints USING btree (chain_id, seq_end);


--
-- Name: ix_audit_chain_prunes_chain_range; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_chain_prunes_chain_range ON public.audit_chain_prunes USING btree (chain_id, seq_min, seq_max);


--
-- Name: ix_audit_chain_prunes_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_chain_prunes_tenant ON public.audit_chain_prunes USING btree (tenant_id, pruned_at);


--
-- Name: ix_audit_export_dest_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_export_dest_tenant_enabled ON public.audit_export_destinations USING btree (tenant_id, enabled);


--
-- Name: ix_audit_export_dest_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_export_dest_tenant_id ON public.audit_export_destinations USING btree (tenant_id);


--
-- Name: ix_audit_export_destinations_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_export_destinations_tenant_id ON public.audit_export_destinations USING btree (tenant_id);


--
-- Name: ix_batch_job_provider_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_provider_batch_id ON public.batch_jobs USING btree (provider_batch_id);


--
-- Name: ix_batch_job_tenant_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_agent ON public.batch_jobs USING btree (tenant_id, agent_id, created_at);


--
-- Name: ix_batch_job_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_created ON public.batch_jobs USING btree (tenant_id, created_at);


--
-- Name: ix_batch_job_tenant_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_group ON public.batch_jobs USING btree (tenant_id, resource_group_id, created_at);


--
-- Name: ix_batch_job_tenant_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_model ON public.batch_jobs USING btree (tenant_id, llm_model_id, created_at);


--
-- Name: ix_batch_job_tenant_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_provider ON public.batch_jobs USING btree (tenant_id, provider, created_at);


--
-- Name: ix_batch_job_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_status ON public.batch_jobs USING btree (tenant_id, status, created_at);


--
-- Name: ix_batch_job_tenant_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_job_tenant_user ON public.batch_jobs USING btree (tenant_id, user_id, created_at);


--
-- Name: ix_batch_req_custom_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_req_custom_id ON public.batch_requests USING btree (tenant_id, batch_job_id, custom_id);


--
-- Name: ix_batch_req_job_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_req_job_status ON public.batch_requests USING btree (batch_job_id, status);


--
-- Name: ix_batch_req_tenant_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batch_req_tenant_job ON public.batch_requests USING btree (tenant_id, batch_job_id, created_at);


--
-- Name: ix_ckr_tenant_check_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ckr_tenant_check_at ON public.assurance_check_results USING btree (tenant_id, check_id, evaluated_at);


--
-- Name: ix_collector_keys_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collector_keys_tenant ON public.collector_keys USING btree (tenant_id);


--
-- Name: ix_compliance_doc_exports_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_compliance_doc_exports_system ON public.compliance_documentation_exports USING btree (tenant_id, ai_system_id, created_at);


--
-- Name: ix_compliance_incident_events_incident; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_compliance_incident_events_incident ON public.compliance_incident_events USING btree (incident_id, at);


--
-- Name: ix_compliance_incidents_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_compliance_incidents_tenant ON public.compliance_incidents USING btree (tenant_id, status, became_aware_at);


--
-- Name: ix_compliance_reports_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_compliance_reports_tenant ON public.compliance_reports USING btree (tenant_id, created_at);


--
-- Name: ix_ctr_config; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ctr_config ON public.semantic_cache_tool_rules USING btree (cache_config_id);


--
-- Name: ix_ctr_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ctr_tenant ON public.semantic_cache_tool_rules USING btree (tenant_id);


--
-- Name: ix_data_erasure_events_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_data_erasure_events_tenant ON public.data_erasure_events USING btree (tenant_id, requested_at);


--
-- Name: ix_disclosure_records_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_disclosure_records_tenant ON public.disclosure_records USING btree (tenant_id, created_at);


--
-- Name: ix_discovered_assets_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovered_assets_tenant ON public.discovered_assets USING btree (tenant_id);


--
-- Name: ix_discovered_assets_tenant_device; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovered_assets_tenant_device ON public.discovered_assets USING btree (tenant_id, device_id);


--
-- Name: ix_discovered_assets_tenant_gov; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovered_assets_tenant_gov ON public.discovered_assets USING btree (tenant_id, governance_status);


--
-- Name: ix_discovered_assets_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovered_assets_tenant_status ON public.discovered_assets USING btree (tenant_id, status);


--
-- Name: ix_discovered_assets_tenant_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovered_assets_tenant_type ON public.discovered_assets USING btree (tenant_id, asset_type);


--
-- Name: ix_discovery_events_asset_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovery_events_asset_key ON public.discovery_events USING btree (tenant_id, asset_key);


--
-- Name: ix_discovery_events_tenant_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovery_events_tenant_received ON public.discovery_events USING btree (tenant_id, received_at);


--
-- Name: ix_discovery_evidence_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovery_evidence_asset ON public.discovery_evidence USING btree (discovered_asset_id);


--
-- Name: ix_discovery_evidence_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_discovery_evidence_tenant ON public.discovery_evidence USING btree (tenant_id);


--
-- Name: ix_docker_deployments_tenant_deployed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_deployed ON public.docker_deployments USING btree (tenant_id, deployed_at);


--
-- Name: ix_docker_deployments_tenant_github; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_github ON public.docker_deployments USING btree (tenant_id, github_repo);


--
-- Name: ix_docker_deployments_tenant_health; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_health ON public.docker_deployments USING btree (tenant_id, health_status, consecutive_failures);


--
-- Name: ix_docker_deployments_tenant_image; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_image ON public.docker_deployments USING btree (tenant_id, image_id);


--
-- Name: ix_docker_deployments_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_name ON public.docker_deployments USING btree (tenant_id, deployment_name);


--
-- Name: ix_docker_deployments_tenant_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_server ON public.docker_deployments USING btree (tenant_id, mcp_server_id);


--
-- Name: ix_docker_deployments_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_status ON public.docker_deployments USING btree (tenant_id, status);


--
-- Name: ix_docker_deployments_tenant_stopped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_deployments_tenant_stopped ON public.docker_deployments USING btree (tenant_id, last_stopped_at);


--
-- Name: ix_docker_images_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_active ON public.docker_images USING btree (tenant_id, is_active);


--
-- Name: ix_docker_images_tenant_build_method; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_build_method ON public.docker_images USING btree (tenant_id, build_method);


--
-- Name: ix_docker_images_tenant_github; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_github ON public.docker_images USING btree (tenant_id, github_repo);


--
-- Name: ix_docker_images_tenant_imported; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_imported ON public.docker_images USING btree (tenant_id, imported_at);


--
-- Name: ix_docker_images_tenant_last_used; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_last_used ON public.docker_images USING btree (tenant_id, last_used);


--
-- Name: ix_docker_images_tenant_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_repo ON public.docker_images USING btree (tenant_id, repository);


--
-- Name: ix_docker_images_tenant_repo_tag; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_docker_images_tenant_repo_tag ON public.docker_images USING btree (tenant_id, repository, tag);


--
-- Name: ix_drift_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_drift_class ON public.ai_system_drift_events USING btree (tenant_id, drift_class, detected_at);


--
-- Name: ix_drift_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_drift_status ON public.ai_system_drift_events USING btree (tenant_id, status, detected_at);


--
-- Name: ix_drift_system_detected; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_drift_system_detected ON public.ai_system_drift_events USING btree (tenant_id, ai_system_id, detected_at);


--
-- Name: ix_ecs_deployments_deployment_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ecs_deployments_deployment_name ON public.ecs_deployments USING btree (deployment_name);


--
-- Name: ix_ecs_deployments_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ecs_deployments_tenant_id ON public.ecs_deployments USING btree (tenant_id);


--
-- Name: ix_ecs_deployments_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ecs_deployments_tenant_status ON public.ecs_deployments USING btree (tenant_id, status);


--
-- Name: ix_end_user_group_members_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_group_members_group ON public.end_user_group_members USING btree (end_user_group_id);


--
-- Name: ix_end_user_group_members_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_group_members_user ON public.end_user_group_members USING btree (end_user_id);


--
-- Name: ix_end_user_groups_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_created_at ON public.resource_groups USING btree (tenant_id, created_at);


--
-- Name: ix_end_user_groups_hierarchy_level; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_hierarchy_level ON public.resource_groups USING btree (tenant_id, hierarchy_level);


--
-- Name: ix_end_user_groups_hierarchy_path; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_hierarchy_path ON public.resource_groups USING btree (hierarchy_path);


--
-- Name: ix_end_user_groups_idp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_idp ON public.end_user_groups USING btree (idp_server_id);


--
-- Name: ix_end_user_groups_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_source ON public.end_user_groups USING btree (source);


--
-- Name: ix_end_user_groups_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant ON public.end_user_groups USING btree (tenant_id);


--
-- Name: ix_end_user_groups_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_active ON public.resource_groups USING btree (tenant_id, is_active);


--
-- Name: ix_end_user_groups_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_id ON public.end_user_groups USING btree (tenant_id);


--
-- Name: ix_end_user_groups_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_name ON public.resource_groups USING btree (tenant_id, name);


--
-- Name: ix_end_user_groups_tenant_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_parent ON public.resource_groups USING btree (tenant_id, parent_group_id);


--
-- Name: ix_end_user_groups_tenant_root; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_root ON public.resource_groups USING btree (tenant_id, parent_group_id) WHERE (parent_group_id IS NULL);


--
-- Name: ix_end_user_groups_tenant_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_type ON public.resource_groups USING btree (tenant_id, group_type);


--
-- Name: ix_end_user_groups_tenant_type_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_groups_tenant_type_active ON public.resource_groups USING btree (tenant_id, group_type, is_active);


--
-- Name: ix_end_user_revoked_tokens_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_user_revoked_tokens_expires_at ON public.end_user_revoked_tokens USING btree (expires_at);


--
-- Name: ix_end_users_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_active ON public.end_users USING btree (tenant_id, is_active);


--
-- Name: ix_end_users_tenant_active_verified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_active_verified ON public.end_users USING btree (tenant_id, is_active, is_verified);


--
-- Name: ix_end_users_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_created ON public.end_users USING btree (tenant_id, created_at);


--
-- Name: ix_end_users_tenant_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_email ON public.end_users USING btree (tenant_id, email);


--
-- Name: ix_end_users_tenant_last_login; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_last_login ON public.end_users USING btree (tenant_id, last_login);


--
-- Name: ix_end_users_tenant_primary_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_primary_group ON public.end_users USING btree (tenant_id, primary_group_id);


--
-- Name: ix_end_users_tenant_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_role ON public.end_users USING btree (tenant_id, role_id);


--
-- Name: ix_end_users_tenant_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_username ON public.end_users USING btree (tenant_id, username);


--
-- Name: ix_end_users_tenant_verified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_end_users_tenant_verified ON public.end_users USING btree (tenant_id, is_verified);


--
-- Name: ix_evidence_bundles_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_bundles_tenant ON public.evidence_bundles USING btree (tenant_id, created_at);


--
-- Name: ix_evidence_receipts_head; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_receipts_head ON public.evidence_receipts USING btree (tree_head_id);


--
-- Name: ix_evidence_tree_heads_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_evidence_tree_heads_tenant ON public.evidence_tree_heads USING btree (tenant_id, issued_at);


--
-- Name: ix_guardrail_break_glass_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_guardrail_break_glass_tenant ON public.guardrail_break_glass USING btree (tenant_id);


--
-- Name: ix_guardrail_break_glass_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_guardrail_break_glass_tenant_active ON public.guardrail_break_glass USING btree (tenant_id, expires_at) WHERE (deactivated_at IS NULL);


--
-- Name: ix_guardrail_configs_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_guardrail_configs_tenant ON public.guardrail_configs USING btree (tenant_id);


--
-- Name: ix_guardrail_configs_tenant_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_guardrail_configs_tenant_default ON public.guardrail_configs USING btree (tenant_id, is_default);


--
-- Name: ix_guardrail_configs_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_guardrail_configs_tenant_name ON public.guardrail_configs USING btree (tenant_id, name);


--
-- Name: ix_health_check_configs_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_health_check_configs_tenant ON public.health_check_configs USING btree (tenant_id);


--
-- Name: ix_idp_servers_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_created_at ON public.idp_servers USING btree (tenant_id, created_at);


--
-- Name: ix_idp_servers_tenant_discovery; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_tenant_discovery ON public.idp_servers USING btree (tenant_id, last_discovered, type);


--
-- Name: ix_idp_servers_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_tenant_enabled ON public.idp_servers USING btree (tenant_id, enabled);


--
-- Name: ix_idp_servers_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_tenant_name ON public.idp_servers USING btree (tenant_id, name);


--
-- Name: ix_idp_servers_tenant_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_tenant_type ON public.idp_servers USING btree (tenant_id, type);


--
-- Name: ix_idp_servers_tenant_type_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_tenant_type_enabled ON public.idp_servers USING btree (tenant_id, type, enabled);


--
-- Name: ix_idp_servers_tenant_verified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_idp_servers_tenant_verified ON public.idp_servers USING btree (tenant_id, verified);


--
-- Name: ix_inbox_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbox_system ON public.assurance_inbox_items USING btree (tenant_id, ai_system_id, state);


--
-- Name: ix_inbox_triage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inbox_triage ON public.assurance_inbox_items USING btree (tenant_id, state, severity, last_seen_at);


--
-- Name: ix_iso42001_coverage_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_iso42001_coverage_tenant ON public.iso42001_control_coverage USING btree (tenant_id);


--
-- Name: ix_kb_connector_run_logs_connector_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kb_connector_run_logs_connector_started ON public.kb_connector_run_logs USING btree (connector_id, started_at);


--
-- Name: ix_kb_connectors_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kb_connectors_due ON public.kb_connectors USING btree (enabled, next_run_at) WHERE enabled;


--
-- Name: ix_kb_connectors_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kb_connectors_tenant ON public.kb_connectors USING btree (tenant_id);


--
-- Name: ix_kb_identity_tenant_type_principal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kb_identity_tenant_type_principal ON public.kb_connector_identity_mappings USING btree (tenant_id, connector_type, source_principal);


--
-- Name: ix_kb_identity_tenant_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kb_identity_tenant_user ON public.kb_connector_identity_mappings USING btree (tenant_id, end_user_id);


--
-- Name: ix_kbc_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbc_tenant ON public.kb_collections USING btree (tenant_id);


--
-- Name: ix_kbc_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_kbc_tenant_name ON public.kb_collections USING btree (tenant_id, name);


--
-- Name: ix_kbch_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbch_document ON public.kb_chunks USING btree (document_id);


--
-- Name: ix_kbch_qdrant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbch_qdrant ON public.kb_chunks USING btree (qdrant_point_id);


--
-- Name: ix_kbch_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbch_tenant ON public.kb_chunks USING btree (tenant_id);


--
-- Name: ix_kbd_collection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbd_collection ON public.kb_documents USING btree (collection_id);


--
-- Name: ix_kbd_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbd_status ON public.kb_documents USING btree (tenant_id, status);


--
-- Name: ix_kbd_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kbd_tenant ON public.kb_documents USING btree (tenant_id);


--
-- Name: ix_kubernetes_clusters_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kubernetes_clusters_tenant_id ON public.kubernetes_clusters USING btree (tenant_id);


--
-- Name: ix_kubernetes_deployments_deployment_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kubernetes_deployments_deployment_name ON public.kubernetes_deployments USING btree (deployment_name);


--
-- Name: ix_kubernetes_deployments_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_kubernetes_deployments_tenant_id ON public.kubernetes_deployments USING btree (tenant_id);


--
-- Name: ix_llm_catalog_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_catalog_mode ON public.llm_catalog USING btree (mode);


--
-- Name: ix_llm_catalog_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_catalog_provider ON public.llm_catalog USING btree (provider_key);


--
-- Name: ix_llm_catalog_recommended; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_catalog_recommended ON public.llm_catalog USING btree (recommended);


--
-- Name: ix_llm_model_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_created_at ON public.llm_model USING btree (tenant_id, created_at);


--
-- Name: ix_llm_model_health_check; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_health_check ON public.llm_model USING btree (tenant_id, last_health_check);


--
-- Name: ix_llm_model_health_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_health_status ON public.llm_model USING btree (tenant_id, health_status, enabled);


--
-- Name: ix_llm_model_latency; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_latency ON public.llm_model USING btree (tenant_id, avg_latency_ms, enabled);


--
-- Name: ix_llm_model_model_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_model_name ON public.llm_model USING btree (tenant_id, model_name, enabled);


--
-- Name: ix_llm_model_parameters; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_parameters ON public.llm_model USING btree (tenant_id, parameters, enabled);


--
-- Name: ix_llm_model_provider_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_provider_id ON public.llm_model USING btree (tenant_id, provider_id, enabled);


--
-- Name: ix_llm_model_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_tenant_enabled ON public.llm_model USING btree (tenant_id, enabled);


--
-- Name: ix_llm_model_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_model_tenant_name ON public.llm_model USING btree (tenant_id, name);


--
-- Name: ix_llm_provider_catalog_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_provider_catalog_category ON public.llm_provider_catalog USING btree (category);


--
-- Name: ix_llm_providers_icon_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_providers_icon_hash ON public.llm_providers USING btree (icon_hash);


--
-- Name: ix_llm_providers_import_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_providers_import_due ON public.llm_providers USING btree (import_next_run_at) WHERE import_enabled;


--
-- Name: ix_llm_providers_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_providers_tenant ON public.llm_providers USING btree (tenant_id);


--
-- Name: ix_llm_providers_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_providers_tenant_enabled ON public.llm_providers USING btree (tenant_id, enabled);


--
-- Name: ix_llm_rg_tier_group_tier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_rg_tier_group_tier ON public.llm_routing_group_tier USING btree (routing_group_id, tier);


--
-- Name: ix_llm_rg_tier_tenant_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_rg_tier_tenant_group ON public.llm_routing_group_tier USING btree (tenant_id, routing_group_id);


--
-- Name: ix_llm_routing_group_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_routing_group_enabled ON public.llm_routing_group USING btree (enabled);


--
-- Name: ix_llm_routing_group_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_routing_group_tenant ON public.llm_routing_group USING btree (tenant_id);


--
-- Name: ix_llm_routing_group_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_routing_group_tenant_enabled ON public.llm_routing_group USING btree (tenant_id, enabled);


--
-- Name: ix_llm_routing_group_tier_llm_model_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_routing_group_tier_llm_model_id ON public.llm_routing_group_tier USING btree (llm_model_id);


--
-- Name: ix_llm_routing_group_tier_routing_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_routing_group_tier_routing_group_id ON public.llm_routing_group_tier USING btree (routing_group_id);


--
-- Name: ix_llm_routing_group_tier_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_routing_group_tier_tenant_id ON public.llm_routing_group_tier USING btree (tenant_id);


--
-- Name: ix_llm_runner_machine_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_runner_machine_tenant ON public.llm_runner_machines USING btree (tenant_id);


--
-- Name: ix_llm_runner_machine_tenant_host; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_runner_machine_tenant_host ON public.llm_runner_machines USING btree (tenant_id, host, port);


--
-- Name: ix_llm_runner_machine_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_llm_runner_machine_tenant_name ON public.llm_runner_machines USING btree (tenant_id, name);


--
-- Name: ix_llm_runner_machine_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_runner_machine_tenant_status ON public.llm_runner_machines USING btree (tenant_id, status);


--
-- Name: ix_mcp_capability_snapshots_server_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_capability_snapshots_server_time ON public.mcp_capability_snapshots USING btree (mcp_server_id, snapshot_at);


--
-- Name: ix_mcp_capability_snapshots_tenant_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_capability_snapshots_tenant_time ON public.mcp_capability_snapshots USING btree (tenant_id, snapshot_at);


--
-- Name: ix_mcp_catalog_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_catalog_created_at ON public.mcp_catalog_entries USING btree (tenant_id, created_at);


--
-- Name: ix_mcp_catalog_global_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_mcp_catalog_global_slug ON public.mcp_catalog_entries USING btree (slug) WHERE (tenant_id IS NULL);


--
-- Name: ix_mcp_catalog_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_catalog_tenant_enabled ON public.mcp_catalog_entries USING btree (tenant_id, enabled);


--
-- Name: ix_mcp_catalog_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_catalog_tenant_name ON public.mcp_catalog_entries USING btree (tenant_id, name);


--
-- Name: ix_mcp_catalog_tenant_runtime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_catalog_tenant_runtime ON public.mcp_catalog_entries USING btree (tenant_id, runtime, enabled);


--
-- Name: ix_mcp_catalog_tenant_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_catalog_tenant_source ON public.mcp_catalog_entries USING btree (tenant_id, source);


--
-- Name: ix_mcp_registry_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_created_at ON public.mcp_registry_configs USING btree (tenant_id, created_at);


--
-- Name: ix_mcp_registry_tenant_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_tenant_default ON public.mcp_registry_configs USING btree (tenant_id, is_default);


--
-- Name: ix_mcp_registry_tenant_en_ver; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_tenant_en_ver ON public.mcp_registry_configs USING btree (tenant_id, enabled, verified);


--
-- Name: ix_mcp_registry_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_tenant_enabled ON public.mcp_registry_configs USING btree (tenant_id, enabled);


--
-- Name: ix_mcp_registry_tenant_last_sync; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_tenant_last_sync ON public.mcp_registry_configs USING btree (tenant_id, last_sync, enabled);


--
-- Name: ix_mcp_registry_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_tenant_name ON public.mcp_registry_configs USING btree (tenant_id, name);


--
-- Name: ix_mcp_registry_tenant_verified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_registry_tenant_verified ON public.mcp_registry_configs USING btree (tenant_id, verified);


--
-- Name: ix_mcp_servers_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_created_at ON public.mcp_servers USING btree (tenant_id, created_at);


--
-- Name: ix_mcp_servers_discovered_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_discovered_at ON public.mcp_servers USING btree (tenant_id, discovered_at);


--
-- Name: ix_mcp_servers_health_check; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_health_check ON public.mcp_servers USING btree (tenant_id, last_health_check);


--
-- Name: ix_mcp_servers_health_failures; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_health_failures ON public.mcp_servers USING btree (consecutive_health_failures, enabled);


--
-- Name: ix_mcp_servers_health_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_health_status ON public.mcp_servers USING btree (tenant_id, health_status, enabled);


--
-- Name: ix_mcp_servers_icon_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_icon_hash ON public.mcp_servers USING btree (icon_hash);


--
-- Name: ix_mcp_servers_idp_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_idp_id ON public.mcp_servers USING btree (idp_id);


--
-- Name: ix_mcp_servers_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_tenant_enabled ON public.mcp_servers USING btree (tenant_id, enabled);


--
-- Name: ix_mcp_servers_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_servers_tenant_name ON public.mcp_servers USING btree (tenant_id, name);


--
-- Name: ix_media_files_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_chat ON public.media_files USING btree (tenant_id, chat_id);


--
-- Name: ix_media_files_lifecycle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_lifecycle ON public.media_files USING btree (expires_at, deleted_at);


--
-- Name: ix_media_files_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_model ON public.media_files USING btree (tenant_id, model_id);


--
-- Name: ix_media_files_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_tenant_active ON public.media_files USING btree (tenant_id, deleted_at);


--
-- Name: ix_ndel_coalesce; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ndel_coalesce ON public.notification_deliveries USING btree (subscription_id, fingerprint, created_at);


--
-- Name: ix_ndel_retry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ndel_retry ON public.notification_deliveries USING btree (status, next_attempt_at);


--
-- Name: ix_notif_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notif_expires ON public.notifications USING btree (expires_at);


--
-- Name: ix_notif_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notif_type ON public.notifications USING btree (tenant_id, notification_type, created_at);


--
-- Name: ix_notif_user_unread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notif_user_unread ON public.notifications USING btree (tenant_id, user_id, is_read, created_at);


--
-- Name: ix_notification_channels_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_channels_tenant_id ON public.notification_channels USING btree (tenant_id);


--
-- Name: ix_notification_deliveries_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_tenant_id ON public.notification_deliveries USING btree (tenant_id);


--
-- Name: ix_notification_subscriptions_channel_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_subscriptions_channel_id ON public.notification_subscriptions USING btree (channel_id);


--
-- Name: ix_notification_subscriptions_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_subscriptions_tenant_id ON public.notification_subscriptions USING btree (tenant_id);


--
-- Name: ix_notifications_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_tenant_id ON public.notifications USING btree (tenant_id);


--
-- Name: ix_oauth_app_clients_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_created_at ON public.oauth_app_clients USING btree (tenant_id, created_at);


--
-- Name: ix_oauth_app_clients_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_active ON public.oauth_app_clients USING btree (tenant_id, is_active);


--
-- Name: ix_oauth_app_clients_tenant_app_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_app_type ON public.oauth_app_clients USING btree (tenant_id, app_type);


--
-- Name: ix_oauth_app_clients_tenant_app_type_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_app_type_active ON public.oauth_app_clients USING btree (tenant_id, app_type, is_active);


--
-- Name: ix_oauth_app_clients_tenant_idp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_idp ON public.oauth_app_clients USING btree (tenant_id, idp_id);


--
-- Name: ix_oauth_app_clients_tenant_idp_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_idp_active ON public.oauth_app_clients USING btree (tenant_id, idp_id, is_active);


--
-- Name: ix_oauth_app_clients_tenant_last_used; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_last_used ON public.oauth_app_clients USING btree (tenant_id, last_used);


--
-- Name: ix_oauth_app_clients_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_app_clients_tenant_name ON public.oauth_app_clients USING btree (tenant_id, name);


--
-- Name: ix_oauth_cimd_clients_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_cimd_clients_tenant_id ON public.oauth_cimd_clients USING btree (tenant_id);


--
-- Name: ix_oauth_tokens_refresh_tracking; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_refresh_tracking ON public.oauth_tokens USING btree (tenant_id, expires_at, revoked);


--
-- Name: ix_oauth_tokens_tenant_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_tenant_expires ON public.oauth_tokens USING btree (tenant_id, expires_at, revoked);


--
-- Name: ix_oauth_tokens_tenant_gateway_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_tenant_gateway_user ON public.oauth_tokens USING btree (tenant_id, gateway_user_id);


--
-- Name: ix_oauth_tokens_tenant_idp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_tenant_idp ON public.oauth_tokens USING btree (tenant_id, idp_id);


--
-- Name: ix_oauth_tokens_tenant_idp_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_tenant_idp_user ON public.oauth_tokens USING btree (tenant_id, idp_user_id);


--
-- Name: ix_oauth_tokens_tenant_revoked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_tenant_revoked ON public.oauth_tokens USING btree (tenant_id, revoked, revoked_at);


--
-- Name: ix_oauth_tokens_tenant_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_tokens_tenant_server ON public.oauth_tokens USING btree (tenant_id, mcp_server_id);


--
-- Name: ix_observed_agent_events_tenant_device; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_observed_agent_events_tenant_device ON public.observed_agent_events USING btree (tenant_id, device_id);


--
-- Name: ix_observed_agent_events_tenant_mcp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_observed_agent_events_tenant_mcp ON public.observed_agent_events USING btree (tenant_id, mcp_server);


--
-- Name: ix_observed_agent_events_tenant_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_observed_agent_events_tenant_session ON public.observed_agent_events USING btree (tenant_id, session_id);


--
-- Name: ix_observed_agent_events_tenant_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_observed_agent_events_tenant_time ON public.observed_agent_events USING btree (tenant_id, occurred_at);


--
-- Name: ix_policy_bundles_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_bundles_tenant ON public.policy_bundles USING btree (tenant_id);


--
-- Name: ix_policy_bundles_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_bundles_tenant_created ON public.policy_bundles USING btree (tenant_id, created_at);


--
-- Name: ix_policy_bundles_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_bundles_tenant_name ON public.policy_bundles USING btree (tenant_id, name);


--
-- Name: ix_policy_bundles_tenant_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_bundles_tenant_scope ON public.policy_bundles USING btree (tenant_id, scope_group_id);


--
-- Name: ix_policy_bundles_tenant_scope_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_bundles_tenant_scope_active ON public.policy_bundles USING btree (tenant_id, scope_group_id, is_active);


--
-- Name: ix_policy_bundles_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_bundles_tenant_status ON public.policy_bundles USING btree (tenant_id, status);


--
-- Name: ix_policy_events_bundle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_events_bundle ON public.policy_bundle_events USING btree (bundle_id);


--
-- Name: ix_policy_events_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_events_tenant ON public.policy_bundle_events USING btree (tenant_id);


--
-- Name: ix_policy_events_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_events_tenant_created ON public.policy_bundle_events USING btree (tenant_id, created_at);


--
-- Name: ix_policy_events_tenant_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_events_tenant_scope ON public.policy_bundle_events USING btree (tenant_id, scope_group_id);


--
-- Name: ix_policy_templates_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_templates_category ON public.policy_templates USING btree (category);


--
-- Name: ix_policy_templates_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_policy_templates_system ON public.policy_templates USING btree (is_system);


--
-- Name: ix_prompt_cache_policies_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_prompt_cache_policies_tenant ON public.prompt_cache_policies USING btree (tenant_id);


--
-- Name: ix_proxy_logs_autonomy_degraded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_autonomy_degraded ON public.proxy_logs USING btree (tenant_id, "timestamp") WHERE ((autonomy_provenance IS NOT NULL) AND ((autonomy_provenance)::text <> 'live'::text));


--
-- Name: ix_proxy_logs_compliance_tags_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_compliance_tags_gin ON public.proxy_logs USING gin (compliance_tags) WHERE (compliance_tags IS NOT NULL);


--
-- Name: ix_proxy_logs_compliance_tenant_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_compliance_tenant_timestamp ON public.proxy_logs USING btree (tenant_id, "timestamp") WHERE (compliance_tags IS NOT NULL);


--
-- Name: ix_proxy_logs_consumed_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_consumed_approval ON public.proxy_logs USING btree (tenant_id, consumed_approval_request_id) WHERE (consumed_approval_request_id IS NOT NULL);


--
-- Name: ix_proxy_logs_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_correlation_id ON public.proxy_logs USING btree (correlation_id);


--
-- Name: ix_proxy_logs_end_user_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_end_user_session_id ON public.proxy_logs USING btree (end_user_session_id);


--
-- Name: ix_proxy_logs_error_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_error_type ON public.proxy_logs USING btree (error_type);


--
-- Name: ix_proxy_logs_gateway_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_gateway_user_id ON public.proxy_logs USING btree (gateway_user_id);


--
-- Name: ix_proxy_logs_gateway_user_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_gateway_user_username ON public.proxy_logs USING btree (gateway_user_username);


--
-- Name: ix_proxy_logs_guardrail_config; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_guardrail_config ON public.proxy_logs USING btree (tenant_id, guardrail_config_id, "timestamp") WHERE (guardrail_config_id IS NOT NULL);


--
-- Name: ix_proxy_logs_guardrails_degraded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_guardrails_degraded ON public.proxy_logs USING btree (tenant_id, "timestamp") WHERE (guardrails_degraded IS NOT NULL);


--
-- Name: ix_proxy_logs_guardrails_skipped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_guardrails_skipped ON public.proxy_logs USING btree (tenant_id, "timestamp") WHERE (guardrails_skipped IS NOT NULL);


--
-- Name: ix_proxy_logs_http_status_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_http_status_code ON public.proxy_logs USING btree (http_status_code);


--
-- Name: ix_proxy_logs_idp_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_idp_server_id ON public.proxy_logs USING btree (idp_server_id);


--
-- Name: ix_proxy_logs_idp_user_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_idp_user_email ON public.proxy_logs USING btree (idp_user_email);


--
-- Name: ix_proxy_logs_idp_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_idp_user_id ON public.proxy_logs USING btree (idp_user_id);


--
-- Name: ix_proxy_logs_idp_user_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_idp_user_username ON public.proxy_logs USING btree (idp_user_username);


--
-- Name: ix_proxy_logs_llm_model_requested; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_llm_model_requested ON public.proxy_logs USING btree (llm_model_requested);


--
-- Name: ix_proxy_logs_llm_model_used; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_llm_model_used ON public.proxy_logs USING btree (llm_model_used);


--
-- Name: ix_proxy_logs_mcp_capability_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_mcp_capability_name ON public.proxy_logs USING btree (mcp_capability_name);


--
-- Name: ix_proxy_logs_mcp_method; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_mcp_method ON public.proxy_logs USING btree (mcp_method);


--
-- Name: ix_proxy_logs_proxy_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_proxy_type ON public.proxy_logs USING btree (proxy_type);


--
-- Name: ix_proxy_logs_record_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_record_id ON public.proxy_logs USING btree (tenant_id, record_id) WHERE (record_id IS NOT NULL);


--
-- Name: ix_proxy_logs_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_request_id ON public.proxy_logs USING btree (request_id);


--
-- Name: ix_proxy_logs_residency_class; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_residency_class ON public.proxy_logs USING btree (tenant_id, residency_class) WHERE (residency_class IS NOT NULL);


--
-- Name: ix_proxy_logs_run_end_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_run_end_state ON public.proxy_logs USING btree (tenant_id, root_task_id) WHERE (run_end_state IS NOT NULL);


--
-- Name: ix_proxy_logs_run_traj; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_run_traj ON public.proxy_logs USING btree (tenant_id, root_task_id, "timestamp", id);


--
-- Name: ix_proxy_logs_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_server_id ON public.proxy_logs USING btree (server_id);


--
-- Name: ix_proxy_logs_skill_execution_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_skill_execution_id ON public.proxy_logs USING btree (skill_execution_id);


--
-- Name: ix_proxy_logs_skill_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_skill_id ON public.proxy_logs USING btree (skill_id);


--
-- Name: ix_proxy_logs_step; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_step ON public.proxy_logs USING btree (tenant_id, root_task_id, step_id) WHERE (step_id IS NOT NULL);


--
-- Name: ix_proxy_logs_success; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_success ON public.proxy_logs USING btree (success);


--
-- Name: ix_proxy_logs_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_sweep ON public.proxy_logs USING btree ("timestamp", tenant_id, root_task_id) INCLUDE (resource_group_id, id) WHERE (root_task_id IS NOT NULL);


--
-- Name: ix_proxy_logs_tenant_actor_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_tenant_actor_agent ON public.proxy_logs USING btree (tenant_id, actor_agent_id, "timestamp");


--
-- Name: ix_proxy_logs_tenant_api_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_tenant_api_key ON public.proxy_logs USING btree (tenant_id, api_key_id);


--
-- Name: ix_proxy_logs_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_tenant_id ON public.proxy_logs USING btree (tenant_id);


--
-- Name: ix_proxy_logs_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_timestamp ON public.proxy_logs USING btree ("timestamp");


--
-- Name: ix_proxy_logs_trace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_trace ON public.proxy_logs USING btree (tenant_id, trace_id) WHERE (trace_id IS NOT NULL);


--
-- Name: ix_proxy_logs_turn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_logs_turn ON public.proxy_logs USING btree (tenant_id, root_task_id, turn_id) WHERE (turn_id IS NOT NULL);


--
-- Name: ix_proxy_metrics_tenant_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_correlation_id ON public.proxy_metrics USING btree (tenant_id, correlation_id);


--
-- Name: ix_proxy_metrics_tenant_dow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_dow ON public.proxy_metrics USING btree (tenant_id, request_day_of_week, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_duration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_duration ON public.proxy_metrics USING btree (tenant_id, total_duration_ms, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_endpoint_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_endpoint_category ON public.proxy_metrics USING btree (tenant_id, endpoint_category, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_error_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_error_timestamp ON public.proxy_metrics USING btree (tenant_id, error_category, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_gateway_user_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_gateway_user_timestamp ON public.proxy_metrics USING btree (tenant_id, gateway_username, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_hour; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_hour ON public.proxy_metrics USING btree (tenant_id, request_hour, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_request_id ON public.proxy_metrics USING btree (tenant_id, request_id);


--
-- Name: ix_proxy_metrics_tenant_server_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_server_timestamp ON public.proxy_metrics USING btree (tenant_id, server_id, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_source_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_source_timestamp ON public.proxy_metrics USING btree (tenant_id, source, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_status_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_status_timestamp ON public.proxy_metrics USING btree (tenant_id, http_status_code, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_timestamp ON public.proxy_metrics USING btree (tenant_id, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_type_success_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_type_success_timestamp ON public.proxy_metrics USING btree (tenant_id, proxy_type, success, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_type_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_type_timestamp ON public.proxy_metrics USING btree (tenant_id, proxy_type, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_user_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_user_timestamp ON public.proxy_metrics USING btree (tenant_id, end_user_username, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_usertype_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_usertype_timestamp ON public.proxy_metrics USING btree (tenant_id, end_user_type, "timestamp");


--
-- Name: ix_proxy_metrics_tenant_vserver_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proxy_metrics_tenant_vserver_timestamp ON public.proxy_metrics USING btree (tenant_id, virtual_server_id, "timestamp");


--
-- Name: ix_record_access_accessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_record_access_accessed ON public.ai_system_record_access USING btree (accessed_at);


--
-- Name: ix_record_access_reach; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_record_access_reach ON public.ai_system_record_access USING btree (tenant_id, ai_system_id, scope_key, accessed_at);


--
-- Name: ix_record_access_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_record_access_run ON public.ai_system_record_access USING btree (tenant_id, root_task_id);


--
-- Name: ix_record_access_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_record_access_subject ON public.ai_system_record_access USING btree (tenant_id, subject_id);


--
-- Name: ix_replay_cases_suite; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_replay_cases_suite ON public.ai_system_replay_cases USING btree (tenant_id, suite_id, enabled);


--
-- Name: ix_replay_runs_suite; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_replay_runs_suite ON public.ai_system_replay_runs USING btree (tenant_id, suite_id, started_at);


--
-- Name: ix_replay_runs_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_replay_runs_system ON public.ai_system_replay_runs USING btree (tenant_id, ai_system_id, started_at);


--
-- Name: ix_replay_suites_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_replay_suites_system ON public.ai_system_replay_suites USING btree (tenant_id, ai_system_id, enabled);


--
-- Name: ix_resource_group_memberships_tenant_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_group_memberships_tenant_group ON public.resource_group_memberships USING btree (tenant_id, group_id);


--
-- Name: ix_resource_group_memberships_tenant_group_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_group_memberships_tenant_group_role ON public.resource_group_memberships USING btree (tenant_id, group_id, role);


--
-- Name: ix_resource_group_memberships_tenant_joined; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_group_memberships_tenant_joined ON public.resource_group_memberships USING btree (tenant_id, joined_at);


--
-- Name: ix_resource_group_memberships_tenant_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_group_memberships_tenant_user ON public.resource_group_memberships USING btree (tenant_id, user_id);


--
-- Name: ix_resource_group_prompt_cache_policies_policy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_group_prompt_cache_policies_policy ON public.resource_group_prompt_cache_policies USING btree (prompt_cache_policy_id);


--
-- Name: ix_resource_groups_parent_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_groups_parent_group_id ON public.resource_groups USING btree (parent_group_id);


--
-- Name: ix_resource_groups_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_resource_groups_tenant_id ON public.resource_groups USING btree (tenant_id);


--
-- Name: ix_response_policies_trigger; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_response_policies_trigger ON public.ai_system_response_policies USING btree (tenant_id, ai_system_id, signal_kind, enabled);


--
-- Name: ix_rg_agent_members_tenant_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rg_agent_members_tenant_agent ON public.resource_group_agent_members USING btree (tenant_id, agent_id);


--
-- Name: ix_rg_agent_members_tenant_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rg_agent_members_tenant_group ON public.resource_group_agent_members USING btree (tenant_id, group_id);


--
-- Name: ix_rg_argpolicies_policy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rg_argpolicies_policy ON public.resource_group_argument_policies USING btree (argument_policy_id);


--
-- Name: ix_rg_end_user_groups_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rg_end_user_groups_group ON public.resource_group_end_user_groups USING btree (end_user_group_id);


--
-- Name: ix_rg_kb_connectors_connector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rg_kb_connectors_connector ON public.resource_group_kb_connectors USING btree (kb_connector_id);


--
-- Name: ix_rg_llm_models_llm_model_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rg_llm_models_llm_model_id ON public.resource_group_llm_models USING btree (llm_model_id);


--
-- Name: ix_rgac_card; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rgac_card ON public.resource_group_agent_cards USING btree (agent_card_id);


--
-- Name: ix_rgac_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rgac_group ON public.resource_group_agent_cards USING btree (resource_group_id);


--
-- Name: ix_rglg_group_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rglg_group_model ON public.resource_group_llm_governance USING btree (resource_group_id, llm_model_id);


--
-- Name: ix_rglg_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rglg_tenant ON public.resource_group_llm_governance USING btree (tenant_id);


--
-- Name: ix_rgmc_group_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rgmc_group_server ON public.resource_group_mcp_capabilities USING btree (resource_group_id, server_config_id);


--
-- Name: ix_rgmc_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rgmc_tenant ON public.resource_group_mcp_capabilities USING btree (tenant_id);


--
-- Name: ix_run_interventions_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_run_interventions_tenant_status ON public.run_interventions USING btree (tenant_id, status);


--
-- Name: ix_saved_log_views_tenant_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saved_log_views_tenant_scope ON public.saved_log_views USING btree (tenant_id, scope);


--
-- Name: ix_saved_log_views_tenant_scope_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_saved_log_views_tenant_scope_owner ON public.saved_log_views USING btree (tenant_id, scope, owner_admin_id);


--
-- Name: ix_scc_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scc_tenant ON public.semantic_cache_configs USING btree (tenant_id);


--
-- Name: ix_scc_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_scc_tenant_name ON public.semantic_cache_configs USING btree (tenant_id, name);


--
-- Name: ix_scheduler_runtime_state_last_beacon_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scheduler_runtime_state_last_beacon_at ON public.scheduler_runtime_state USING btree (last_beacon_at);


--
-- Name: ix_scim_tokens_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scim_tokens_hash ON public.scim_tokens USING btree (token_hash);


--
-- Name: ix_scim_tokens_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scim_tokens_tenant_active ON public.scim_tokens USING btree (tenant_id, revoked_at);


--
-- Name: ix_scim_tokens_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scim_tokens_tenant_id ON public.scim_tokens USING btree (tenant_id);


--
-- Name: ix_search_provider_configs_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_provider_configs_tenant ON public.search_provider_configs USING btree (tenant_id);


--
-- Name: ix_semantic_policies_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_semantic_policies_tenant ON public.semantic_policies USING btree (tenant_id);


--
-- Name: ix_semantic_policies_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_semantic_policies_tenant_enabled ON public.semantic_policies USING btree (tenant_id, enabled);


--
-- Name: ix_server_configs_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_default ON public.server_configs USING btree (is_default);


--
-- Name: ix_server_configs_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_enabled ON public.server_configs USING btree (enabled);


--
-- Name: ix_server_configs_oauth_client; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_oauth_client ON public.server_configs USING btree (oauth_client_id);


--
-- Name: ix_server_configs_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_priority ON public.server_configs USING btree (priority);


--
-- Name: ix_server_configs_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_server ON public.server_configs USING btree (mcp_server_id);


--
-- Name: ix_server_configs_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_tenant ON public.server_configs USING btree (tenant_id);


--
-- Name: ix_server_configs_virtual_server; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_server_configs_virtual_server ON public.server_configs USING btree (virtual_server_id);


--
-- Name: ix_session_budget_resets_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_session_budget_resets_tenant ON public.session_budget_resets USING btree (tenant_id);


--
-- Name: ix_skill_exec_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_parent ON public.agent_skill_executions USING btree (parent_execution_id);


--
-- Name: ix_skill_exec_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_request_id ON public.agent_skill_executions USING btree (request_id);


--
-- Name: ix_skill_exec_tenant_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_approval ON public.agent_skill_executions USING btree (tenant_id, approval_status, started_at);


--
-- Name: ix_skill_exec_tenant_correlation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_correlation ON public.agent_skill_executions USING btree (tenant_id, correlation_id);


--
-- Name: ix_skill_exec_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_created ON public.agent_skill_executions USING btree (tenant_id, created_at);


--
-- Name: ix_skill_exec_tenant_group_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_group_started ON public.agent_skill_executions USING btree (tenant_id, group_id, started_at);


--
-- Name: ix_skill_exec_tenant_skill_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_skill_started ON public.agent_skill_executions USING btree (tenant_id, skill_id, started_at);


--
-- Name: ix_skill_exec_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_status ON public.agent_skill_executions USING btree (tenant_id, execution_status, started_at);


--
-- Name: ix_skill_exec_tenant_user_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_exec_tenant_user_started ON public.agent_skill_executions USING btree (tenant_id, user_id, started_at);


--
-- Name: ix_skill_sources_last_synced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_sources_last_synced_at ON public.skill_sources USING btree (last_synced_at);


--
-- Name: ix_skill_sources_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_skill_sources_tenant_status ON public.skill_sources USING btree (tenant_id, enabled, status);


--
-- Name: ix_srs_last_beacon_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_srs_last_beacon_at ON public.scheduler_runtime_state USING btree (last_beacon_at);


--
-- Name: ix_system_admin_audit_logs_action_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_audit_logs_action_timestamp ON public.system_admin_audit_logs USING btree (action, "timestamp");


--
-- Name: ix_system_admin_audit_logs_admin_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_audit_logs_admin_timestamp ON public.system_admin_audit_logs USING btree (admin_id, "timestamp");


--
-- Name: ix_system_admin_audit_logs_affected_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_audit_logs_affected_tenant ON public.system_admin_audit_logs USING btree (affected_tenant_id, "timestamp");


--
-- Name: ix_system_admin_audit_logs_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_audit_logs_ip ON public.system_admin_audit_logs USING btree (ip_address, "timestamp");


--
-- Name: ix_system_admin_audit_logs_success_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_audit_logs_success_timestamp ON public.system_admin_audit_logs USING btree (success, "timestamp");


--
-- Name: ix_system_admin_audit_logs_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_audit_logs_timestamp ON public.system_admin_audit_logs USING btree ("timestamp");


--
-- Name: ix_system_admin_sessions_admin_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_sessions_admin_active ON public.system_admin_sessions USING btree (admin_id, is_active);


--
-- Name: ix_system_admin_sessions_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_sessions_expires ON public.system_admin_sessions USING btree (expires_at);


--
-- Name: ix_system_admin_sessions_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_sessions_ip ON public.system_admin_sessions USING btree (ip_address);


--
-- Name: ix_system_admin_sessions_risk; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_sessions_risk ON public.system_admin_sessions USING btree (risk_score);


--
-- Name: ix_system_admin_sessions_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admin_sessions_token_hash ON public.system_admin_sessions USING btree (token_hash);


--
-- Name: ix_system_admins_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admins_active ON public.system_admins USING btree (is_active);


--
-- Name: ix_system_admins_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admins_created ON public.system_admins USING btree (created_at);


--
-- Name: ix_system_admins_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admins_email ON public.system_admins USING btree (email);


--
-- Name: ix_system_admins_locked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admins_locked ON public.system_admins USING btree (is_locked);


--
-- Name: ix_system_admins_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_admins_username ON public.system_admins USING btree (username);


--
-- Name: ix_system_alerts_category_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_alerts_category_status ON public.system_alerts USING btree (category, status);


--
-- Name: ix_system_alerts_status_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_alerts_status_last_seen ON public.system_alerts USING btree (status, last_seen_at);


--
-- Name: ix_system_alerts_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_alerts_tenant_id ON public.system_alerts USING btree (tenant_id);


--
-- Name: ix_system_alerts_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_alerts_tenant_status ON public.system_alerts USING btree (tenant_id, status);


--
-- Name: ix_system_operation_log_action_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_operation_log_action_created ON public.system_operation_log USING btree (action, created_at);


--
-- Name: ix_system_operation_log_actor_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_operation_log_actor_created ON public.system_operation_log USING btree (actor_admin_id, created_at);


--
-- Name: ix_system_operation_log_payload_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_operation_log_payload_gin ON public.system_operation_log USING gin (payload);


--
-- Name: ix_system_operation_log_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_system_operation_log_tenant_created ON public.system_operation_log USING btree (tenant_id, created_at);


--
-- Name: ix_tar_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tar_group ON public.tool_approval_requests USING btree (tenant_id, requester_group_id, status);


--
-- Name: ix_tar_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tar_pending ON public.tool_approval_requests USING btree (tenant_id, status, expires_at);


--
-- Name: ix_tar_requester; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tar_requester ON public.tool_approval_requests USING btree (tenant_id, requester_user_id, created_at);


--
-- Name: ix_tar_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tar_token ON public.tool_approval_requests USING btree (approval_token);


--
-- Name: ix_tar_trace_root; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tar_trace_root ON public.tool_approval_requests USING btree (tenant_id, originating_trace_root_id);


--
-- Name: ix_tenant_admin_audit_logs_tenant_action_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_audit_logs_tenant_action_timestamp ON public.tenant_admin_audit_logs USING btree (tenant_id, action, "timestamp");


--
-- Name: ix_tenant_admin_audit_logs_tenant_admin_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_audit_logs_tenant_admin_timestamp ON public.tenant_admin_audit_logs USING btree (tenant_id, admin_id, "timestamp");


--
-- Name: ix_tenant_admin_audit_logs_tenant_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_audit_logs_tenant_ip ON public.tenant_admin_audit_logs USING btree (tenant_id, ip_address, "timestamp");


--
-- Name: ix_tenant_admin_audit_logs_tenant_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_audit_logs_tenant_resource ON public.tenant_admin_audit_logs USING btree (tenant_id, resource_type, resource_id, "timestamp");


--
-- Name: ix_tenant_admin_audit_logs_tenant_success_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_audit_logs_tenant_success_timestamp ON public.tenant_admin_audit_logs USING btree (tenant_id, success, "timestamp");


--
-- Name: ix_tenant_admin_audit_logs_tenant_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_audit_logs_tenant_timestamp ON public.tenant_admin_audit_logs USING btree (tenant_id, "timestamp");


--
-- Name: ix_tenant_admin_sessions_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_sessions_tenant_active ON public.tenant_admin_sessions USING btree (tenant_id, is_active);


--
-- Name: ix_tenant_admin_sessions_tenant_admin_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_sessions_tenant_admin_active ON public.tenant_admin_sessions USING btree (tenant_id, admin_id, is_active);


--
-- Name: ix_tenant_admin_sessions_tenant_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_sessions_tenant_expires ON public.tenant_admin_sessions USING btree (tenant_id, expires_at);


--
-- Name: ix_tenant_admin_sessions_tenant_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_sessions_tenant_ip ON public.tenant_admin_sessions USING btree (tenant_id, ip_address);


--
-- Name: ix_tenant_admin_sessions_tenant_risk; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_sessions_tenant_risk ON public.tenant_admin_sessions USING btree (tenant_id, risk_score);


--
-- Name: ix_tenant_admin_sessions_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admin_sessions_token_hash ON public.tenant_admin_sessions USING btree (token_hash);


--
-- Name: ix_tenant_admins_idp_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_idp_lookup ON public.tenant_admins USING btree (tenant_id, idp_server_id, idp_subject);


--
-- Name: ix_tenant_admins_tenant_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_active ON public.tenant_admins USING btree (tenant_id, is_active);


--
-- Name: ix_tenant_admins_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_created ON public.tenant_admins USING btree (tenant_id, created_at);


--
-- Name: ix_tenant_admins_tenant_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_email ON public.tenant_admins USING btree (tenant_id, email);


--
-- Name: ix_tenant_admins_tenant_failed_logins; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_failed_logins ON public.tenant_admins USING btree (tenant_id, failed_login_attempts);


--
-- Name: ix_tenant_admins_tenant_locked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_locked ON public.tenant_admins USING btree (tenant_id, is_locked);


--
-- Name: ix_tenant_admins_tenant_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_role ON public.tenant_admins USING btree (tenant_id, role_id);


--
-- Name: ix_tenant_admins_tenant_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_admins_tenant_username ON public.tenant_admins USING btree (tenant_id, username);


--
-- Name: ix_tenant_role_permissions_permission; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_role_permissions_permission ON public.tenant_role_permissions USING btree (permission);


--
-- Name: ix_tenant_role_permissions_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_role_permissions_role ON public.tenant_role_permissions USING btree (role_id);


--
-- Name: ix_tenant_roles_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_roles_tenant ON public.tenant_roles USING btree (tenant_id);


--
-- Name: ix_tenant_roles_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_roles_tenant_name ON public.tenant_roles USING btree (tenant_id, name);


--
-- Name: ix_tenant_roles_tenant_system; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenant_roles_tenant_system ON public.tenant_roles USING btree (tenant_id, is_system);


--
-- Name: ix_tenants_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_active ON public.tenants USING btree (is_active);


--
-- Name: ix_tenants_billing_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_billing_plan ON public.tenants USING btree (billing_plan);


--
-- Name: ix_tenants_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_created ON public.tenants USING btree (created_at);


--
-- Name: ix_tenants_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_default ON public.tenants USING btree (is_default);


--
-- Name: ix_tenants_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_domain ON public.tenants USING btree (domain);


--
-- Name: ix_tenants_last_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_last_active ON public.tenants USING btree (last_active_at);


--
-- Name: ix_tenants_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_name ON public.tenants USING btree (name);


--
-- Name: ix_tenants_suspended; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_suspended ON public.tenants USING btree (suspended);


--
-- Name: ix_tenants_trial; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tenants_trial ON public.tenants USING btree (is_trial, trial_ends_at);


--
-- Name: ix_tool_approval_requests_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tool_approval_requests_tenant_id ON public.tool_approval_requests USING btree (tenant_id);


--
-- Name: ix_upl_llm_cache_hit_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_llm_cache_hit_timestamp ON public.proxy_logs USING btree (tenant_id, llm_cache_hit, "timestamp");


--
-- Name: ix_upl_llm_cost_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_llm_cost_timestamp ON public.proxy_logs USING btree (tenant_id, llm_estimated_cost_usd, "timestamp");


--
-- Name: ix_upl_llm_fallback_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_llm_fallback_timestamp ON public.proxy_logs USING btree (tenant_id, llm_used_fallback, "timestamp");


--
-- Name: ix_upl_llm_model_req_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_llm_model_req_timestamp ON public.proxy_logs USING btree (tenant_id, llm_model_requested, "timestamp");


--
-- Name: ix_upl_llm_model_used_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_llm_model_used_timestamp ON public.proxy_logs USING btree (tenant_id, llm_model_used, "timestamp");


--
-- Name: ix_upl_llm_provider_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_llm_provider_timestamp ON public.proxy_logs USING btree (tenant_id, llm_provider, "timestamp");


--
-- Name: ix_upl_mcp_capability_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_mcp_capability_timestamp ON public.proxy_logs USING btree (tenant_id, mcp_capability_name, "timestamp");


--
-- Name: ix_upl_mcp_method_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_mcp_method_timestamp ON public.proxy_logs USING btree (tenant_id, mcp_method, "timestamp");


--
-- Name: ix_upl_mcp_server_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_mcp_server_timestamp ON public.proxy_logs USING btree (tenant_id, proxy_type, server_id, "timestamp");


--
-- Name: ix_upl_oauth_flow_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_oauth_flow_timestamp ON public.proxy_logs USING btree (tenant_id, oauth_flow_type, "timestamp");


--
-- Name: ix_upl_oauth_idp_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_oauth_idp_timestamp ON public.proxy_logs USING btree (tenant_id, idp_server_id, "timestamp");


--
-- Name: ix_upl_skill_execution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_skill_execution ON public.proxy_logs USING btree (skill_execution_id);


--
-- Name: ix_upl_tenant_correlation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_correlation ON public.proxy_logs USING btree (tenant_id, correlation_id, "timestamp");


--
-- Name: ix_upl_tenant_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_created_at ON public.proxy_logs USING btree (tenant_id, created_at, id);


--
-- Name: ix_upl_tenant_duration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_duration ON public.proxy_logs USING btree (tenant_id, duration_ms, "timestamp");


--
-- Name: ix_upl_tenant_error_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_error_type ON public.proxy_logs USING btree (tenant_id, error_type, "timestamp");


--
-- Name: ix_upl_tenant_gateway_user_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_gateway_user_timestamp ON public.proxy_logs USING btree (tenant_id, gateway_user_id, "timestamp");


--
-- Name: ix_upl_tenant_gateway_username_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_gateway_username_timestamp ON public.proxy_logs USING btree (tenant_id, gateway_user_username, "timestamp");


--
-- Name: ix_upl_tenant_idp_user_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_idp_user_timestamp ON public.proxy_logs USING btree (tenant_id, idp_user_id, "timestamp");


--
-- Name: ix_upl_tenant_idp_username_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_idp_username_timestamp ON public.proxy_logs USING btree (tenant_id, idp_user_username, "timestamp");


--
-- Name: ix_upl_tenant_parent_request; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_parent_request ON public.proxy_logs USING btree (tenant_id, parent_request_id, "timestamp");


--
-- Name: ix_upl_tenant_proxy_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_proxy_timestamp ON public.proxy_logs USING btree (tenant_id, proxy_type, "timestamp");


--
-- Name: ix_upl_tenant_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_request_id ON public.proxy_logs USING btree (tenant_id, request_id);


--
-- Name: ix_upl_tenant_session_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_session_timestamp ON public.proxy_logs USING btree (tenant_id, end_user_session_id, "timestamp");


--
-- Name: ix_upl_tenant_skill_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_skill_timestamp ON public.proxy_logs USING btree (tenant_id, skill_id, "timestamp");


--
-- Name: ix_upl_tenant_status_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_status_timestamp ON public.proxy_logs USING btree (tenant_id, http_status_code, "timestamp");


--
-- Name: ix_upl_tenant_success_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_success_timestamp ON public.proxy_logs USING btree (tenant_id, success, "timestamp");


--
-- Name: ix_upl_tenant_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upl_tenant_timestamp ON public.proxy_logs USING btree (tenant_id, "timestamp");


--
-- Name: ix_usage_alerts_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_alerts_active ON public.usage_alerts USING btree (tenant_id, status);


--
-- Name: ix_usage_alerts_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_alerts_group ON public.usage_alerts USING btree (tenant_id, group_id, created_at);


--
-- Name: ix_usage_alerts_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_alerts_period ON public.usage_alerts USING btree (status, period, period_start);


--
-- Name: ix_usage_alerts_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_alerts_tenant_id ON public.usage_alerts USING btree (tenant_id);


--
-- Name: ix_usage_alerts_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_alerts_type ON public.usage_alerts USING btree (tenant_id, alert_type, created_at);


--
-- Name: ix_usage_ancestry_rollup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_ancestry_rollup ON public.usage_logs USING btree (tenant_id, ancestry_path, created_at);


--
-- Name: ix_usage_cost_analysis; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_cost_analysis ON public.usage_logs USING btree (tenant_id, estimated_cost, created_at);


--
-- Name: ix_usage_error_tracking; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_error_tracking ON public.usage_logs USING btree (tenant_id, status_code, created_at);


--
-- Name: ix_usage_import_checkpoints_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_import_checkpoints_tenant ON public.usage_import_checkpoints USING btree (tenant_id);


--
-- Name: ix_usage_import_runs_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_import_runs_provider ON public.usage_import_run_logs USING btree (tenant_id, provider_id, started_at);


--
-- Name: ix_usage_inheritance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_inheritance ON public.usage_logs USING btree (tenant_id, is_inherited, created_at);


--
-- Name: ix_usage_logs_source_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_logs_source_created ON public.usage_logs USING btree (tenant_id, source, created_at);


--
-- Name: ix_usage_mcp_tools; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_mcp_tools ON public.usage_logs USING btree (tenant_id, mcp_tool_name, created_at);


--
-- Name: ix_usage_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_provider ON public.usage_logs USING btree (tenant_id, provider, created_at);


--
-- Name: ix_usage_quota_check; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_quota_check ON public.usage_logs USING btree (tenant_id, group_id, resource_type, created_at);


--
-- Name: ix_usage_request; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_request ON public.usage_logs USING btree (request_id);


--
-- Name: ix_usage_resource_analysis; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_resource_analysis ON public.usage_logs USING btree (tenant_id, resource_type, resource_id, created_at);


--
-- Name: ix_usage_skill_execution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_skill_execution ON public.usage_logs USING btree (skill_execution_id);


--
-- Name: ix_usage_skill_quota; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_skill_quota ON public.usage_logs USING btree (tenant_id, group_id, skill_id, created_at);


--
-- Name: ix_usage_user_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_user_time ON public.usage_logs USING btree (tenant_id, user_id, created_at);


--
-- Name: ix_video_jobs_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_jobs_chat ON public.video_generation_jobs USING btree (tenant_id, chat_id);


--
-- Name: ix_video_jobs_poller; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_jobs_poller ON public.video_generation_jobs USING btree (status, updated_at);


--
-- Name: ix_video_jobs_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_jobs_tenant_status ON public.video_generation_jobs USING btree (tenant_id, status);


--
-- Name: ix_virtual_servers_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_servers_created_at ON public.virtual_servers USING btree (tenant_id, created_at);


--
-- Name: ix_virtual_servers_tenant_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_servers_tenant_enabled ON public.virtual_servers USING btree (tenant_id, enabled);


--
-- Name: ix_virtual_servers_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_servers_tenant_name ON public.virtual_servers USING btree (tenant_id, name);


--
-- Name: ix_virtual_servers_tenant_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_servers_tenant_owner ON public.virtual_servers USING btree (tenant_id, owner);


--
-- Name: ix_virtual_servers_tenant_owner_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_servers_tenant_owner_enabled ON public.virtual_servers USING btree (tenant_id, owner, enabled);


--
-- Name: ix_witnesses_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_witnesses_tenant ON public.witnesses USING btree (tenant_id);


--
-- Name: uq_achk_tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_achk_tenant_name ON public.assurance_checks USING btree (tenant_id, name);


--
-- Name: uq_action_records_log_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_action_records_log_seq ON public.action_records USING btree (log_id, seq);


--
-- Name: uq_agent_identity_approval_history_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_agent_identity_approval_history_current ON public.agent_identity_approval_history USING btree (agent_id) WHERE (valid_to IS NULL);


--
-- Name: uq_ai_system_contracts_one_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ai_system_contracts_one_active ON public.ai_system_contracts USING btree (tenant_id, ai_system_id) WHERE ((status)::text = 'active'::text);


--
-- Name: uq_ai_system_lifecycle_events_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ai_system_lifecycle_events_subject ON public.ai_system_lifecycle_events USING btree (tenant_id, ai_system_id, event_type, subject_id) WHERE (subject_id IS NOT NULL);


--
-- Name: uq_assurance_check_reviews_sample; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_assurance_check_reviews_sample ON public.assurance_check_reviews USING btree (check_id, evaluator_fingerprint, sample_key);


--
-- Name: uq_assurance_check_rubrics_period; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_assurance_check_rubrics_period ON public.assurance_check_rubrics USING btree (check_id, rubric_digest, period_start);


--
-- Name: uq_ckr_check_version_run; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ckr_check_version_run ON public.assurance_check_results USING btree (check_id, check_version, run_id) WHERE ((run_id IS NOT NULL) AND (NOT backtest));


--
-- Name: uq_ckv_check_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ckv_check_version ON public.assurance_check_versions USING btree (check_id, version);


--
-- Name: uq_compliance_notice_configs_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_compliance_notice_configs_scope ON public.compliance_notice_configs USING btree (tenant_id, COALESCE(ai_system_id, ''::character varying));


--
-- Name: uq_compliance_reports_period; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_compliance_reports_period ON public.compliance_reports USING btree (tenant_id, framework_id, COALESCE(ai_system_id, ''::character varying), cadence, period_start);


--
-- Name: uq_dig_tenant_week; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_dig_tenant_week ON public.assurance_digests USING btree (tenant_id, week_start);


--
-- Name: uq_evidence_signing_keys_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_evidence_signing_keys_active ON public.evidence_signing_keys USING btree (tenant_id) WHERE ((status)::text = 'active'::text);


--
-- Name: uq_evidence_tree_heads_log_size; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_evidence_tree_heads_log_size ON public.evidence_tree_heads USING btree (log_id, tree_size);


--
-- Name: uq_inbox_open_fingerprint; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_inbox_open_fingerprint ON public.assurance_inbox_items USING btree (tenant_id, fingerprint) WHERE ((state)::text <> 'resolved'::text);


--
-- Name: uq_kb_documents_connector_doc_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_kb_documents_connector_doc_key ON public.kb_documents USING btree (connector_doc_key) WHERE (connector_doc_key IS NOT NULL);


--
-- Name: uq_ndel_subscription_event; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ndel_subscription_event ON public.notification_deliveries USING btree (subscription_id, inbox_event_id);


--
-- Name: uq_policy_bundles_active_per_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_policy_bundles_active_per_scope ON public.policy_bundles USING btree (tenant_id, scope_group_id) WHERE (is_active = true);


--
-- Name: uq_proxy_logs_chain_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_proxy_logs_chain_seq ON public.proxy_logs USING btree (chain_id, chain_seq) WHERE (chain_id IS NOT NULL);


--
-- Name: uq_system_alerts_active_dedup; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_system_alerts_active_dedup ON public.system_alerts USING btree (dedup_key) WHERE ((status)::text = ANY ((ARRAY['active'::character varying, 'acknowledged'::character varying])::text[]));


--
-- Name: a2a_push_notification_configs a2a_push_notification_configs_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_push_notification_configs
    ADD CONSTRAINT a2a_push_notification_configs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.a2a_tasks(id) ON DELETE CASCADE;


--
-- Name: a2a_push_notification_configs a2a_push_notification_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_push_notification_configs
    ADD CONSTRAINT a2a_push_notification_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: a2a_tasks a2a_tasks_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_tasks
    ADD CONSTRAINT a2a_tasks_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: a2a_tasks a2a_tasks_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_tasks
    ADD CONSTRAINT a2a_tasks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_card_capabilities agent_card_capabilities_agent_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_card_capabilities
    ADD CONSTRAINT agent_card_capabilities_agent_card_id_fkey FOREIGN KEY (agent_card_id) REFERENCES public.agent_cards(id) ON DELETE CASCADE;


--
-- Name: agent_cards agent_cards_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_cards
    ADD CONSTRAINT agent_cards_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_grants agent_grants_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_grants
    ADD CONSTRAINT agent_grants_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_identities(id) ON DELETE CASCADE;


--
-- Name: agent_grants agent_grants_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_grants
    ADD CONSTRAINT agent_grants_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_identities agent_identities_idp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identities
    ADD CONSTRAINT agent_identities_idp_server_id_fkey FOREIGN KEY (idp_server_id) REFERENCES public.idp_servers(id) ON DELETE SET NULL;


--
-- Name: agent_identities agent_identities_owner_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identities
    ADD CONSTRAINT agent_identities_owner_user_id_fkey FOREIGN KEY (owner_user_id) REFERENCES public.end_users(id) ON DELETE SET NULL;


--
-- Name: agent_identities agent_identities_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identities
    ADD CONSTRAINT agent_identities_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_identity_approval_history agent_identity_approval_history_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identity_approval_history
    ADD CONSTRAINT agent_identity_approval_history_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_identities(id) ON DELETE CASCADE;


--
-- Name: agent_identity_approval_history agent_identity_approval_history_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_identity_approval_history
    ADD CONSTRAINT agent_identity_approval_history_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_implementation_releases agent_implementation_releases_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_implementation_releases
    ADD CONSTRAINT agent_implementation_releases_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_identities(id) ON DELETE CASCADE;


--
-- Name: agent_implementation_releases agent_implementation_releases_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_implementation_releases
    ADD CONSTRAINT agent_implementation_releases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_skill_assets agent_skill_assets_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_assets
    ADD CONSTRAINT agent_skill_assets_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.agent_skills(id) ON DELETE CASCADE;


--
-- Name: agent_skill_assets agent_skill_assets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_assets
    ADD CONSTRAINT agent_skill_assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_skill_dependencies agent_skill_dependencies_dependency_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_dependencies
    ADD CONSTRAINT agent_skill_dependencies_dependency_skill_id_fkey FOREIGN KEY (dependency_skill_id) REFERENCES public.agent_skills(id) ON DELETE CASCADE;


--
-- Name: agent_skill_dependencies agent_skill_dependencies_parent_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_dependencies
    ADD CONSTRAINT agent_skill_dependencies_parent_skill_id_fkey FOREIGN KEY (parent_skill_id) REFERENCES public.agent_skills(id) ON DELETE CASCADE;


--
-- Name: agent_skill_dependencies agent_skill_dependencies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_dependencies
    ADD CONSTRAINT agent_skill_dependencies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_skill_executions agent_skill_executions_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_executions
    ADD CONSTRAINT agent_skill_executions_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: agent_skill_executions agent_skill_executions_parent_execution_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_executions
    ADD CONSTRAINT agent_skill_executions_parent_execution_id_fkey FOREIGN KEY (parent_execution_id) REFERENCES public.agent_skill_executions(id) ON DELETE SET NULL;


--
-- Name: agent_skill_executions agent_skill_executions_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_executions
    ADD CONSTRAINT agent_skill_executions_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.agent_skills(id) ON DELETE SET NULL;


--
-- Name: agent_skill_executions agent_skill_executions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_executions
    ADD CONSTRAINT agent_skill_executions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_skill_executions agent_skill_executions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_executions
    ADD CONSTRAINT agent_skill_executions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.end_users(id) ON DELETE SET NULL;


--
-- Name: agent_skill_resources agent_skill_resources_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT agent_skill_resources_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.agent_skills(id) ON DELETE CASCADE;


--
-- Name: agent_skill_resources agent_skill_resources_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT agent_skill_resources_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_skill_scripts agent_skill_scripts_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_scripts
    ADD CONSTRAINT agent_skill_scripts_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.agent_skills(id) ON DELETE CASCADE;


--
-- Name: agent_skill_scripts agent_skill_scripts_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_scripts
    ADD CONSTRAINT agent_skill_scripts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: agent_skills agent_skills_source_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT agent_skills_source_repo_id_fkey FOREIGN KEY (source_repo_id) REFERENCES public.skill_sources(id) ON DELETE SET NULL;


--
-- Name: agent_skills agent_skills_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT agent_skills_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_asset_factsheets ai_asset_factsheets_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_asset_factsheets
    ADD CONSTRAINT ai_asset_factsheets_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.ai_assets(id) ON DELETE CASCADE;


--
-- Name: ai_asset_factsheets ai_asset_factsheets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_asset_factsheets
    ADD CONSTRAINT ai_asset_factsheets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_assets ai_assets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_assets
    ADD CONSTRAINT ai_assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_assurance_reports ai_system_assurance_reports_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_assurance_reports
    ADD CONSTRAINT ai_system_assurance_reports_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_assurance_reports ai_system_assurance_reports_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_assurance_reports
    ADD CONSTRAINT ai_system_assurance_reports_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_baseline_epochs ai_system_baseline_epochs_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baseline_epochs
    ADD CONSTRAINT ai_system_baseline_epochs_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_baseline_epochs ai_system_baseline_epochs_drift_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baseline_epochs
    ADD CONSTRAINT ai_system_baseline_epochs_drift_event_id_fkey FOREIGN KEY (drift_event_id) REFERENCES public.ai_system_drift_events(id) ON DELETE SET NULL;


--
-- Name: ai_system_baseline_epochs ai_system_baseline_epochs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baseline_epochs
    ADD CONSTRAINT ai_system_baseline_epochs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_baselines ai_system_baselines_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baselines
    ADD CONSTRAINT ai_system_baselines_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_baselines ai_system_baselines_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_baselines
    ADD CONSTRAINT ai_system_baselines_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_compliance_profiles ai_system_compliance_profiles_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_compliance_profiles
    ADD CONSTRAINT ai_system_compliance_profiles_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_contracts ai_system_contracts_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_contracts
    ADD CONSTRAINT ai_system_contracts_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_contracts ai_system_contracts_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_contracts
    ADD CONSTRAINT ai_system_contracts_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_dependencies ai_system_dependencies_agent_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT ai_system_dependencies_agent_card_id_fkey FOREIGN KEY (agent_card_id) REFERENCES public.agent_cards(id) ON DELETE CASCADE;


--
-- Name: ai_system_dependencies ai_system_dependencies_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT ai_system_dependencies_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_dependencies ai_system_dependencies_depends_on_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT ai_system_dependencies_depends_on_system_id_fkey FOREIGN KEY (depends_on_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_dependencies ai_system_dependencies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_dependencies
    ADD CONSTRAINT ai_system_dependencies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_drift_events ai_system_drift_events_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_drift_events
    ADD CONSTRAINT ai_system_drift_events_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_drift_events ai_system_drift_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_drift_events
    ADD CONSTRAINT ai_system_drift_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_health ai_system_health_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_health
    ADD CONSTRAINT ai_system_health_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_health ai_system_health_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_health
    ADD CONSTRAINT ai_system_health_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_incident_window_findings ai_system_incident_window_findings_drift_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_window_findings
    ADD CONSTRAINT ai_system_incident_window_findings_drift_event_id_fkey FOREIGN KEY (drift_event_id) REFERENCES public.ai_system_drift_events(id) ON DELETE CASCADE;


--
-- Name: ai_system_incident_window_findings ai_system_incident_window_findings_incident_window_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_window_findings
    ADD CONSTRAINT ai_system_incident_window_findings_incident_window_id_fkey FOREIGN KEY (incident_window_id) REFERENCES public.ai_system_incident_windows(id) ON DELETE CASCADE;


--
-- Name: ai_system_incident_window_findings ai_system_incident_window_findings_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_window_findings
    ADD CONSTRAINT ai_system_incident_window_findings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_incident_windows ai_system_incident_windows_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_windows
    ADD CONSTRAINT ai_system_incident_windows_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_incident_windows ai_system_incident_windows_drift_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_windows
    ADD CONSTRAINT ai_system_incident_windows_drift_event_id_fkey FOREIGN KEY (drift_event_id) REFERENCES public.ai_system_drift_events(id) ON DELETE SET NULL;


--
-- Name: ai_system_incident_windows ai_system_incident_windows_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_incident_windows
    ADD CONSTRAINT ai_system_incident_windows_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_lifecycle_events ai_system_lifecycle_events_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_lifecycle_events
    ADD CONSTRAINT ai_system_lifecycle_events_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_lifecycle_events ai_system_lifecycle_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_lifecycle_events
    ADD CONSTRAINT ai_system_lifecycle_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_liveness_expectations ai_system_liveness_expectations_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_liveness_expectations
    ADD CONSTRAINT ai_system_liveness_expectations_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_liveness_expectations ai_system_liveness_expectations_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_liveness_expectations
    ADD CONSTRAINT ai_system_liveness_expectations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_cases ai_system_replay_cases_source_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_cases
    ADD CONSTRAINT ai_system_replay_cases_source_run_id_fkey FOREIGN KEY (source_run_id) REFERENCES public.ai_system_runs(id) ON DELETE SET NULL;


--
-- Name: ai_system_replay_cases ai_system_replay_cases_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_cases
    ADD CONSTRAINT ai_system_replay_cases_suite_id_fkey FOREIGN KEY (suite_id) REFERENCES public.ai_system_replay_suites(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_cases ai_system_replay_cases_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_cases
    ADD CONSTRAINT ai_system_replay_cases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_runs ai_system_replay_runs_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_runs
    ADD CONSTRAINT ai_system_replay_runs_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_runs ai_system_replay_runs_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_runs
    ADD CONSTRAINT ai_system_replay_runs_suite_id_fkey FOREIGN KEY (suite_id) REFERENCES public.ai_system_replay_suites(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_runs ai_system_replay_runs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_runs
    ADD CONSTRAINT ai_system_replay_runs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_suites ai_system_replay_suites_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_suites
    ADD CONSTRAINT ai_system_replay_suites_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_replay_suites ai_system_replay_suites_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_replay_suites
    ADD CONSTRAINT ai_system_replay_suites_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_response_policies ai_system_response_policies_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_response_policies
    ADD CONSTRAINT ai_system_response_policies_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_response_policies ai_system_response_policies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_response_policies
    ADD CONSTRAINT ai_system_response_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_run_segments ai_system_run_segments_root_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT ai_system_run_segments_root_system_id_fkey FOREIGN KEY (root_system_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: ai_system_run_segments ai_system_run_segments_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT ai_system_run_segments_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.ai_system_runs(id) ON DELETE CASCADE;


--
-- Name: ai_system_run_segments ai_system_run_segments_segment_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT ai_system_run_segments_segment_system_id_fkey FOREIGN KEY (segment_system_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: ai_system_run_segments ai_system_run_segments_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT ai_system_run_segments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ai_system_runs ai_system_runs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_runs
    ADD CONSTRAINT ai_system_runs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_identities(id) ON DELETE SET NULL;


--
-- Name: ai_system_runs ai_system_runs_ai_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_runs
    ADD CONSTRAINT ai_system_runs_ai_system_id_fkey FOREIGN KEY (ai_system_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: ai_system_runs ai_system_runs_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_runs
    ADD CONSTRAINT ai_system_runs_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: ai_system_runs ai_system_runs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_runs
    ADD CONSTRAINT ai_system_runs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_identities(id) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.end_users(id) ON DELETE CASCADE;


--
-- Name: argument_policies argument_policies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.argument_policies
    ADD CONSTRAINT argument_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: assurance_check_results assurance_check_results_check_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_results
    ADD CONSTRAINT assurance_check_results_check_id_fkey FOREIGN KEY (check_id) REFERENCES public.assurance_checks(id) ON DELETE CASCADE;


--
-- Name: assurance_check_reviews assurance_check_reviews_check_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_reviews
    ADD CONSTRAINT assurance_check_reviews_check_id_fkey FOREIGN KEY (check_id) REFERENCES public.assurance_checks(id) ON DELETE CASCADE;


--
-- Name: assurance_check_rubrics assurance_check_rubrics_check_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_rubrics
    ADD CONSTRAINT assurance_check_rubrics_check_id_fkey FOREIGN KEY (check_id) REFERENCES public.assurance_checks(id) ON DELETE CASCADE;


--
-- Name: assurance_check_versions assurance_check_versions_check_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_check_versions
    ADD CONSTRAINT assurance_check_versions_check_id_fkey FOREIGN KEY (check_id) REFERENCES public.assurance_checks(id) ON DELETE CASCADE;


--
-- Name: assurance_inbox_events assurance_inbox_events_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assurance_inbox_events
    ADD CONSTRAINT assurance_inbox_events_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.assurance_inbox_items(id) ON DELETE CASCADE;


--
-- Name: audit_export_destinations audit_export_destinations_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_export_destinations
    ADD CONSTRAINT audit_export_destinations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: batch_jobs batch_jobs_llm_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_jobs
    ADD CONSTRAINT batch_jobs_llm_model_id_fkey FOREIGN KEY (llm_model_id) REFERENCES public.llm_model(id) ON DELETE SET NULL;


--
-- Name: batch_jobs batch_jobs_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_jobs
    ADD CONSTRAINT batch_jobs_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: batch_jobs batch_jobs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_jobs
    ADD CONSTRAINT batch_jobs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: batch_jobs batch_jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_jobs
    ADD CONSTRAINT batch_jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.end_users(id) ON DELETE SET NULL;


--
-- Name: batch_requests batch_requests_batch_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_requests
    ADD CONSTRAINT batch_requests_batch_job_id_fkey FOREIGN KEY (batch_job_id) REFERENCES public.batch_jobs(id) ON DELETE CASCADE;


--
-- Name: batch_requests batch_requests_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_requests
    ADD CONSTRAINT batch_requests_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: collector_keys collector_keys_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collector_keys
    ADD CONSTRAINT collector_keys_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: compliance_incident_events compliance_incident_events_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_incident_events
    ADD CONSTRAINT compliance_incident_events_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.compliance_incidents(id) ON DELETE CASCADE;


--
-- Name: discovered_assets discovered_assets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovered_assets
    ADD CONSTRAINT discovered_assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: discovery_events discovery_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovery_events
    ADD CONSTRAINT discovery_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: discovery_evidence discovery_evidence_discovered_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovery_evidence
    ADD CONSTRAINT discovery_evidence_discovered_asset_id_fkey FOREIGN KEY (discovered_asset_id) REFERENCES public.discovered_assets(id) ON DELETE CASCADE;


--
-- Name: discovery_evidence discovery_evidence_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovery_evidence
    ADD CONSTRAINT discovery_evidence_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: docker_deployments docker_deployments_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT docker_deployments_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.docker_images(id) ON DELETE SET NULL;


--
-- Name: docker_deployments docker_deployments_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT docker_deployments_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE SET NULL;


--
-- Name: docker_deployments docker_deployments_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_deployments
    ADD CONSTRAINT docker_deployments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: docker_images docker_images_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.docker_images
    ADD CONSTRAINT docker_images_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: ecs_deployments ecs_deployments_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecs_deployments
    ADD CONSTRAINT ecs_deployments_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id);


--
-- Name: ecs_deployments ecs_deployments_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecs_deployments
    ADD CONSTRAINT ecs_deployments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: end_user_group_members end_user_group_members_end_user_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_group_members
    ADD CONSTRAINT end_user_group_members_end_user_group_id_fkey FOREIGN KEY (end_user_group_id) REFERENCES public.end_user_groups(id) ON DELETE CASCADE;


--
-- Name: end_user_group_members end_user_group_members_end_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_group_members
    ADD CONSTRAINT end_user_group_members_end_user_id_fkey FOREIGN KEY (end_user_id) REFERENCES public.end_users(id) ON DELETE CASCADE;


--
-- Name: end_user_groups end_user_groups_idp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_groups
    ADD CONSTRAINT end_user_groups_idp_server_id_fkey FOREIGN KEY (idp_server_id) REFERENCES public.idp_servers(id) ON DELETE SET NULL;


--
-- Name: end_user_groups end_user_groups_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_user_groups
    ADD CONSTRAINT end_user_groups_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: end_users end_users_idp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT end_users_idp_server_id_fkey FOREIGN KEY (idp_server_id) REFERENCES public.idp_servers(id) ON DELETE SET NULL;


--
-- Name: end_users end_users_primary_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT end_users_primary_group_id_fkey FOREIGN KEY (primary_group_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: end_users end_users_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT end_users_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.tenant_roles(id) ON DELETE SET NULL;


--
-- Name: end_users end_users_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.end_users
    ADD CONSTRAINT end_users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: evidence_receipts evidence_receipts_tree_head_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evidence_receipts
    ADD CONSTRAINT evidence_receipts_tree_head_id_fkey FOREIGN KEY (tree_head_id) REFERENCES public.evidence_tree_heads(id) ON DELETE CASCADE;


--
-- Name: ai_system_evidence fk_ai_system_evidence_dependency; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_evidence
    ADD CONSTRAINT fk_ai_system_evidence_dependency FOREIGN KEY (dependency_id) REFERENCES public.ai_system_dependencies(id) ON DELETE SET NULL;


--
-- Name: ai_system_run_segments fk_ai_system_run_segments_caller_system; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_system_run_segments
    ADD CONSTRAINT fk_ai_system_run_segments_caller_system FOREIGN KEY (caller_system_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: guardrail_break_glass guardrail_break_glass_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrail_break_glass
    ADD CONSTRAINT guardrail_break_glass_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: guardrail_configs guardrail_configs_scope_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrail_configs
    ADD CONSTRAINT guardrail_configs_scope_mcp_server_id_fkey FOREIGN KEY (scope_mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE SET NULL;


--
-- Name: guardrail_configs guardrail_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardrail_configs
    ADD CONSTRAINT guardrail_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: health_check_configs health_check_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.health_check_configs
    ADD CONSTRAINT health_check_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: idp_servers idp_servers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.idp_servers
    ADD CONSTRAINT idp_servers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: iso42001_control_coverage iso42001_control_coverage_control_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso42001_control_coverage
    ADD CONSTRAINT iso42001_control_coverage_control_id_fkey FOREIGN KEY (control_id) REFERENCES public.iso42001_controls(control_id) ON DELETE CASCADE;


--
-- Name: iso42001_control_coverage iso42001_control_coverage_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.iso42001_control_coverage
    ADD CONSTRAINT iso42001_control_coverage_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: kb_chunks kb_chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_chunks
    ADD CONSTRAINT kb_chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.kb_documents(id) ON DELETE CASCADE;


--
-- Name: kb_collections kb_collections_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_collections
    ADD CONSTRAINT kb_collections_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: kb_connector_checkpoints kb_connector_checkpoints_connector_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_checkpoints
    ADD CONSTRAINT kb_connector_checkpoints_connector_id_fkey FOREIGN KEY (connector_id) REFERENCES public.kb_connectors(id) ON DELETE CASCADE;


--
-- Name: kb_connector_credentials kb_connector_credentials_connector_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_credentials
    ADD CONSTRAINT kb_connector_credentials_connector_id_fkey FOREIGN KEY (connector_id) REFERENCES public.kb_connectors(id) ON DELETE CASCADE;


--
-- Name: kb_connector_identity_mappings kb_connector_identity_mappings_end_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_identity_mappings
    ADD CONSTRAINT kb_connector_identity_mappings_end_user_id_fkey FOREIGN KEY (end_user_id) REFERENCES public.end_users(id) ON DELETE CASCADE;


--
-- Name: kb_connector_identity_mappings kb_connector_identity_mappings_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_identity_mappings
    ADD CONSTRAINT kb_connector_identity_mappings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: kb_connector_run_logs kb_connector_run_logs_connector_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connector_run_logs
    ADD CONSTRAINT kb_connector_run_logs_connector_id_fkey FOREIGN KEY (connector_id) REFERENCES public.kb_connectors(id) ON DELETE CASCADE;


--
-- Name: kb_connectors kb_connectors_collection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connectors
    ADD CONSTRAINT kb_connectors_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES public.kb_collections(id) ON DELETE SET NULL;


--
-- Name: kb_connectors kb_connectors_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_connectors
    ADD CONSTRAINT kb_connectors_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: kb_documents kb_documents_collection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_documents
    ADD CONSTRAINT kb_documents_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES public.kb_collections(id) ON DELETE CASCADE;


--
-- Name: kb_documents kb_documents_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_documents
    ADD CONSTRAINT kb_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: kubernetes_clusters kubernetes_clusters_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kubernetes_clusters
    ADD CONSTRAINT kubernetes_clusters_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: kubernetes_deployments kubernetes_deployments_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kubernetes_deployments
    ADD CONSTRAINT kubernetes_deployments_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.docker_images(id);


--
-- Name: kubernetes_deployments kubernetes_deployments_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kubernetes_deployments
    ADD CONSTRAINT kubernetes_deployments_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id);


--
-- Name: kubernetes_deployments kubernetes_deployments_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kubernetes_deployments
    ADD CONSTRAINT kubernetes_deployments_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: llm_model llm_model_catalog_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_model
    ADD CONSTRAINT llm_model_catalog_id_fkey FOREIGN KEY (catalog_id) REFERENCES public.llm_catalog(id) ON DELETE SET NULL;


--
-- Name: llm_model llm_model_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_model
    ADD CONSTRAINT llm_model_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.llm_providers(id) ON DELETE RESTRICT;


--
-- Name: llm_model llm_model_routing_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_model
    ADD CONSTRAINT llm_model_routing_group_id_fkey FOREIGN KEY (routing_group_id) REFERENCES public.llm_routing_group(id) ON DELETE SET NULL;


--
-- Name: llm_model llm_model_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_model
    ADD CONSTRAINT llm_model_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: llm_providers llm_providers_catalog_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_providers
    ADD CONSTRAINT llm_providers_catalog_id_fkey FOREIGN KEY (catalog_id) REFERENCES public.llm_provider_catalog(id) ON DELETE SET NULL;


--
-- Name: llm_providers llm_providers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_providers
    ADD CONSTRAINT llm_providers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: llm_routing_group llm_routing_group_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_routing_group
    ADD CONSTRAINT llm_routing_group_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: llm_routing_group_tier llm_routing_group_tier_llm_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_routing_group_tier
    ADD CONSTRAINT llm_routing_group_tier_llm_model_id_fkey FOREIGN KEY (llm_model_id) REFERENCES public.llm_model(id) ON DELETE CASCADE;


--
-- Name: llm_routing_group_tier llm_routing_group_tier_routing_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_routing_group_tier
    ADD CONSTRAINT llm_routing_group_tier_routing_group_id_fkey FOREIGN KEY (routing_group_id) REFERENCES public.llm_routing_group(id) ON DELETE CASCADE;


--
-- Name: llm_runner_machines llm_runner_machines_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_runner_machines
    ADD CONSTRAINT llm_runner_machines_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: mcp_capability_snapshots mcp_capability_snapshots_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_capability_snapshots
    ADD CONSTRAINT mcp_capability_snapshots_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: mcp_capability_snapshots mcp_capability_snapshots_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_capability_snapshots
    ADD CONSTRAINT mcp_capability_snapshots_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: mcp_catalog_entries mcp_catalog_entries_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_catalog_entries
    ADD CONSTRAINT mcp_catalog_entries_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: mcp_registry_configs mcp_registry_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_registry_configs
    ADD CONSTRAINT mcp_registry_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: mcp_servers mcp_servers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: media_files media_files_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.llm_model(id) ON DELETE SET NULL;


--
-- Name: media_files media_files_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: notification_deliveries notification_deliveries_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.notification_subscriptions(id) ON DELETE CASCADE;


--
-- Name: notification_subscriptions notification_subscriptions_channel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_subscriptions
    ADD CONSTRAINT notification_subscriptions_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.notification_channels(id) ON DELETE CASCADE;


--
-- Name: oauth_app_clients oauth_app_clients_idp_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_app_clients
    ADD CONSTRAINT oauth_app_clients_idp_id_fkey FOREIGN KEY (idp_id) REFERENCES public.idp_servers(id) ON DELETE CASCADE;


--
-- Name: oauth_app_clients oauth_app_clients_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_app_clients
    ADD CONSTRAINT oauth_app_clients_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: oauth_tokens oauth_tokens_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: observed_agent_events observed_agent_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observed_agent_events
    ADD CONSTRAINT observed_agent_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: policy_bundle_events policy_bundle_events_bundle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_bundle_events
    ADD CONSTRAINT policy_bundle_events_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES public.policy_bundles(id) ON DELETE CASCADE;


--
-- Name: policy_bundle_events policy_bundle_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_bundle_events
    ADD CONSTRAINT policy_bundle_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: policy_bundles policy_bundles_scope_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_bundles
    ADD CONSTRAINT policy_bundles_scope_group_id_fkey FOREIGN KEY (scope_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: policy_bundles policy_bundles_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_bundles
    ADD CONSTRAINT policy_bundles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: policy_templates policy_templates_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policy_templates
    ADD CONSTRAINT policy_templates_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: prompt_cache_policies prompt_cache_policies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prompt_cache_policies
    ADD CONSTRAINT prompt_cache_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: proxy_logs proxy_logs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_logs
    ADD CONSTRAINT proxy_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: proxy_metrics proxy_metrics_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_metrics
    ADD CONSTRAINT proxy_metrics_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_cards resource_group_agent_cards_agent_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_cards
    ADD CONSTRAINT resource_group_agent_cards_agent_card_id_fkey FOREIGN KEY (agent_card_id) REFERENCES public.agent_cards(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_cards resource_group_agent_cards_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_cards
    ADD CONSTRAINT resource_group_agent_cards_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_members resource_group_agent_members_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_members
    ADD CONSTRAINT resource_group_agent_members_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_identities(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_members resource_group_agent_members_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_members
    ADD CONSTRAINT resource_group_agent_members_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_members resource_group_agent_members_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_members
    ADD CONSTRAINT resource_group_agent_members_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_skills resource_group_agent_skills_agent_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_skills
    ADD CONSTRAINT resource_group_agent_skills_agent_skill_id_fkey FOREIGN KEY (agent_skill_id) REFERENCES public.agent_skills(id) ON DELETE CASCADE;


--
-- Name: resource_group_agent_skills resource_group_agent_skills_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_agent_skills
    ADD CONSTRAINT resource_group_agent_skills_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_argument_policies resource_group_argument_policies_argument_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_argument_policies
    ADD CONSTRAINT resource_group_argument_policies_argument_policy_id_fkey FOREIGN KEY (argument_policy_id) REFERENCES public.argument_policies(id) ON DELETE CASCADE;


--
-- Name: resource_group_argument_policies resource_group_argument_policies_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_argument_policies
    ADD CONSTRAINT resource_group_argument_policies_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_cache_configs resource_group_cache_configs_cache_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_cache_configs
    ADD CONSTRAINT resource_group_cache_configs_cache_config_id_fkey FOREIGN KEY (cache_config_id) REFERENCES public.semantic_cache_configs(id) ON DELETE CASCADE;


--
-- Name: resource_group_cache_configs resource_group_cache_configs_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_cache_configs
    ADD CONSTRAINT resource_group_cache_configs_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_deployment_machines resource_group_deployment_machines_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_deployment_machines
    ADD CONSTRAINT resource_group_deployment_machines_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.llm_runner_machines(id) ON DELETE CASCADE;


--
-- Name: resource_group_deployment_machines resource_group_deployment_machines_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_deployment_machines
    ADD CONSTRAINT resource_group_deployment_machines_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_end_user_groups resource_group_end_user_groups_end_user_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_end_user_groups
    ADD CONSTRAINT resource_group_end_user_groups_end_user_group_id_fkey FOREIGN KEY (end_user_group_id) REFERENCES public.end_user_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_end_user_groups resource_group_end_user_groups_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_end_user_groups
    ADD CONSTRAINT resource_group_end_user_groups_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_guardrails resource_group_guardrails_guardrail_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_guardrails
    ADD CONSTRAINT resource_group_guardrails_guardrail_config_id_fkey FOREIGN KEY (guardrail_config_id) REFERENCES public.guardrail_configs(id) ON DELETE CASCADE;


--
-- Name: resource_group_guardrails resource_group_guardrails_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_guardrails
    ADD CONSTRAINT resource_group_guardrails_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_kb_collections resource_group_kb_collections_kb_collection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_kb_collections
    ADD CONSTRAINT resource_group_kb_collections_kb_collection_id_fkey FOREIGN KEY (kb_collection_id) REFERENCES public.kb_collections(id) ON DELETE CASCADE;


--
-- Name: resource_group_kb_collections resource_group_kb_collections_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_kb_collections
    ADD CONSTRAINT resource_group_kb_collections_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_kb_connectors resource_group_kb_connectors_kb_connector_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_kb_connectors
    ADD CONSTRAINT resource_group_kb_connectors_kb_connector_id_fkey FOREIGN KEY (kb_connector_id) REFERENCES public.kb_connectors(id) ON DELETE CASCADE;


--
-- Name: resource_group_kb_connectors resource_group_kb_connectors_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_kb_connectors
    ADD CONSTRAINT resource_group_kb_connectors_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_llm_governance resource_group_llm_governance_llm_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_governance
    ADD CONSTRAINT resource_group_llm_governance_llm_model_id_fkey FOREIGN KEY (llm_model_id) REFERENCES public.llm_model(id) ON DELETE CASCADE;


--
-- Name: resource_group_llm_governance resource_group_llm_governance_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_governance
    ADD CONSTRAINT resource_group_llm_governance_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_llm_governance resource_group_llm_governance_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_governance
    ADD CONSTRAINT resource_group_llm_governance_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: resource_group_llm_models resource_group_llm_models_llm_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_models
    ADD CONSTRAINT resource_group_llm_models_llm_model_id_fkey FOREIGN KEY (llm_model_id) REFERENCES public.llm_model(id) ON DELETE CASCADE;


--
-- Name: resource_group_llm_models resource_group_llm_models_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_llm_models
    ADD CONSTRAINT resource_group_llm_models_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_mcp_capabilities resource_group_mcp_capabilities_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_mcp_capabilities
    ADD CONSTRAINT resource_group_mcp_capabilities_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_mcp_capabilities resource_group_mcp_capabilities_server_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_mcp_capabilities
    ADD CONSTRAINT resource_group_mcp_capabilities_server_config_id_fkey FOREIGN KEY (server_config_id) REFERENCES public.server_configs(id) ON DELETE CASCADE;


--
-- Name: resource_group_mcp_capabilities resource_group_mcp_capabilities_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_mcp_capabilities
    ADD CONSTRAINT resource_group_mcp_capabilities_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: resource_group_memberships resource_group_memberships_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_memberships
    ADD CONSTRAINT resource_group_memberships_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_memberships resource_group_memberships_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_memberships
    ADD CONSTRAINT resource_group_memberships_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: resource_group_memberships resource_group_memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_memberships
    ADD CONSTRAINT resource_group_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.end_users(id) ON DELETE CASCADE;


--
-- Name: resource_group_prompt_cache_policies resource_group_prompt_cache_policie_prompt_cache_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_prompt_cache_policies
    ADD CONSTRAINT resource_group_prompt_cache_policie_prompt_cache_policy_id_fkey FOREIGN KEY (prompt_cache_policy_id) REFERENCES public.prompt_cache_policies(id) ON DELETE CASCADE;


--
-- Name: resource_group_prompt_cache_policies resource_group_prompt_cache_policies_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_prompt_cache_policies
    ADD CONSTRAINT resource_group_prompt_cache_policies_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.llm_model(id) ON DELETE CASCADE;


--
-- Name: resource_group_prompt_cache_policies resource_group_prompt_cache_policies_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_prompt_cache_policies
    ADD CONSTRAINT resource_group_prompt_cache_policies_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_semantic_policies resource_group_semantic_policies_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_semantic_policies
    ADD CONSTRAINT resource_group_semantic_policies_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_semantic_policies resource_group_semantic_policies_semantic_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_semantic_policies
    ADD CONSTRAINT resource_group_semantic_policies_semantic_policy_id_fkey FOREIGN KEY (semantic_policy_id) REFERENCES public.semantic_policies(id) ON DELETE CASCADE;


--
-- Name: resource_group_server_configs resource_group_server_configs_resource_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_server_configs
    ADD CONSTRAINT resource_group_server_configs_resource_group_id_fkey FOREIGN KEY (resource_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_group_server_configs resource_group_server_configs_server_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_group_server_configs
    ADD CONSTRAINT resource_group_server_configs_server_config_id_fkey FOREIGN KEY (server_config_id) REFERENCES public.server_configs(id) ON DELETE CASCADE;


--
-- Name: resource_groups resource_groups_parent_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_groups
    ADD CONSTRAINT resource_groups_parent_group_id_fkey FOREIGN KEY (parent_group_id) REFERENCES public.resource_groups(id) ON DELETE CASCADE;


--
-- Name: resource_groups resource_groups_successor_system_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_groups
    ADD CONSTRAINT resource_groups_successor_system_id_fkey FOREIGN KEY (successor_system_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: resource_groups resource_groups_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_groups
    ADD CONSTRAINT resource_groups_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: router_cache_configs router_cache_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.router_cache_configs
    ADD CONSTRAINT router_cache_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: run_interventions run_interventions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.run_interventions
    ADD CONSTRAINT run_interventions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: saved_log_views saved_log_views_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_log_views
    ADD CONSTRAINT saved_log_views_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: scim_tokens scim_tokens_idp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scim_tokens
    ADD CONSTRAINT scim_tokens_idp_server_id_fkey FOREIGN KEY (idp_server_id) REFERENCES public.idp_servers(id) ON DELETE SET NULL;


--
-- Name: scim_tokens scim_tokens_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scim_tokens
    ADD CONSTRAINT scim_tokens_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: search_provider_configs search_provider_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_provider_configs
    ADD CONSTRAINT search_provider_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: semantic_cache_configs semantic_cache_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_configs
    ADD CONSTRAINT semantic_cache_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: semantic_cache_tool_rules semantic_cache_tool_rules_cache_config_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_tool_rules
    ADD CONSTRAINT semantic_cache_tool_rules_cache_config_id_fkey FOREIGN KEY (cache_config_id) REFERENCES public.semantic_cache_configs(id) ON DELETE CASCADE;


--
-- Name: semantic_cache_tool_rules semantic_cache_tool_rules_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_cache_tool_rules
    ADD CONSTRAINT semantic_cache_tool_rules_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: semantic_policies semantic_policies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.semantic_policies
    ADD CONSTRAINT semantic_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: server_configs server_configs_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_configs
    ADD CONSTRAINT server_configs_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: server_configs server_configs_oauth_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_configs
    ADD CONSTRAINT server_configs_oauth_client_id_fkey FOREIGN KEY (oauth_client_id) REFERENCES public.oauth_app_clients(id) ON DELETE CASCADE;


--
-- Name: server_configs server_configs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_configs
    ADD CONSTRAINT server_configs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: server_configs server_configs_virtual_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_configs
    ADD CONSTRAINT server_configs_virtual_server_id_fkey FOREIGN KEY (virtual_server_id) REFERENCES public.virtual_servers(id) ON DELETE CASCADE;


--
-- Name: skill_sources skill_sources_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skill_sources
    ADD CONSTRAINT skill_sources_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: system_admin_audit_logs system_admin_audit_logs_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admin_audit_logs
    ADD CONSTRAINT system_admin_audit_logs_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.system_admins(id) ON DELETE SET NULL;


--
-- Name: system_admin_sessions system_admin_sessions_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_admin_sessions
    ADD CONSTRAINT system_admin_sessions_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.system_admins(id) ON DELETE CASCADE;


--
-- Name: system_operation_log system_operation_log_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_operation_log
    ADD CONSTRAINT system_operation_log_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_a2a_keys tenant_a2a_keys_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_a2a_keys
    ADD CONSTRAINT tenant_a2a_keys_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_admin_audit_logs tenant_admin_audit_logs_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admin_audit_logs
    ADD CONSTRAINT tenant_admin_audit_logs_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.tenant_admins(id) ON DELETE SET NULL;


--
-- Name: tenant_admin_audit_logs tenant_admin_audit_logs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admin_audit_logs
    ADD CONSTRAINT tenant_admin_audit_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_admin_sessions tenant_admin_sessions_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admin_sessions
    ADD CONSTRAINT tenant_admin_sessions_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.tenant_admins(id) ON DELETE CASCADE;


--
-- Name: tenant_admin_sessions tenant_admin_sessions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admin_sessions
    ADD CONSTRAINT tenant_admin_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_admins tenant_admins_idp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admins
    ADD CONSTRAINT tenant_admins_idp_server_id_fkey FOREIGN KEY (idp_server_id) REFERENCES public.idp_servers(id) ON DELETE SET NULL;


--
-- Name: tenant_admins tenant_admins_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admins
    ADD CONSTRAINT tenant_admins_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.tenant_roles(id) ON DELETE SET NULL;


--
-- Name: tenant_admins tenant_admins_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_admins
    ADD CONSTRAINT tenant_admins_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenant_role_permissions tenant_role_permissions_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_role_permissions
    ADD CONSTRAINT tenant_role_permissions_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.tenant_roles(id) ON DELETE CASCADE;


--
-- Name: tenant_roles tenant_roles_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_roles
    ADD CONSTRAINT tenant_roles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: tenants tenants_default_embedding_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_default_embedding_model_id_fkey FOREIGN KEY (default_embedding_model_id) REFERENCES public.llm_model(id) ON DELETE SET NULL;


--
-- Name: tenants tenants_default_judge_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_default_judge_model_id_fkey FOREIGN KEY (default_judge_model_id) REFERENCES public.llm_model(id) ON DELETE SET NULL;


--
-- Name: usage_import_checkpoints usage_import_checkpoints_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_import_checkpoints
    ADD CONSTRAINT usage_import_checkpoints_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.llm_providers(id) ON DELETE CASCADE;


--
-- Name: usage_import_checkpoints usage_import_checkpoints_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_import_checkpoints
    ADD CONSTRAINT usage_import_checkpoints_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: usage_import_run_logs usage_import_run_logs_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_import_run_logs
    ADD CONSTRAINT usage_import_run_logs_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.llm_providers(id) ON DELETE CASCADE;


--
-- Name: usage_import_run_logs usage_import_run_logs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_import_run_logs
    ADD CONSTRAINT usage_import_run_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: usage_logs usage_logs_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.resource_groups(id) ON DELETE SET NULL;


--
-- Name: usage_logs usage_logs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: usage_logs usage_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.end_users(id) ON DELETE SET NULL;


--
-- Name: video_generation_jobs video_generation_jobs_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_generation_jobs
    ADD CONSTRAINT video_generation_jobs_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.llm_model(id) ON DELETE SET NULL;


--
-- Name: video_generation_jobs video_generation_jobs_output_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_generation_jobs
    ADD CONSTRAINT video_generation_jobs_output_file_id_fkey FOREIGN KEY (output_file_id) REFERENCES public.media_files(id) ON DELETE SET NULL;


--
-- Name: video_generation_jobs video_generation_jobs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_generation_jobs
    ADD CONSTRAINT video_generation_jobs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: virtual_servers virtual_servers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.virtual_servers
    ADD CONSTRAINT virtual_servers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--



-- Restore default search_path so sqlx can write _sqlx_migrations to public.
SELECT pg_catalog.set_config('search_path', 'public', false);
