-- GENERATED FILE — DO NOT EDIT.
--
-- Produced by `tests/schema/gen-test-superset.py` from
-- `tests/schema/schema.sql`. Re-run that script after regenerating the
-- fixture; CI fails if this file is stale.
--
-- Every table the application knows about, permissive: NOT NULL and
-- foreign keys are omitted so tests can insert narrow rows. See the
-- script's docstring for why each piece is here.



-- Enum types. CREATE TYPE has no IF NOT EXISTS, so each is
-- wrapped in a DO block that swallows duplicate_object.

DO $$ BEGIN
    CREATE TYPE approvalstatus AS ENUM ('NOT_REQUIRED', 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE assettype AS ENUM ('TEMPLATE', 'IMAGE', 'DATA');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE dependencytype AS ENUM ('REQUIRED', 'OPTIONAL', 'CONDITIONAL');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE executionstatus AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE grouptype AS ENUM ('ORGANIZATION', 'DIVISION', 'DEPARTMENT', 'BUSINESS_UNIT', 'TEAM', 'PROJECT', 'PROGRAM', 'INITIATIVE', 'AI_SYSTEM', 'CUSTOM');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE policybundlestatus AS ENUM ('DRAFT', 'VALIDATED', 'ACTIVE', 'SUPERSEDED', 'ROLLED_BACK');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE policyeventtype AS ENUM ('VALIDATED', 'APPLIED', 'ROLLED_BACK', 'DRY_RUN', 'SUPERSEDED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE scriptexecutionmode AS ENUM ('SANDBOX', 'TRUSTED', 'DISABLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE scriptlanguage AS ENUM ('PYTHON', 'JAVASCRIPT', 'TYPESCRIPT', 'BASH');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE skilldisclosuremode AS ENUM ('PROGRESSIVE', 'EXECUTE', 'BOTH');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE skillresourcetype AS ENUM ('REFERENCE', 'FORM', 'SCHEMA', 'TEMPLATE', 'EXAMPLE', 'ASSET');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE skillstatus AS ENUM ('DRAFT', 'VALIDATED', 'PUBLISHED', 'DEPRECATED', 'ARCHIVED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- Tables.

CREATE TABLE IF NOT EXISTS a2a_push_notification_configs (
    id character varying(64),
    tenant_id character varying(255),
    task_id character varying(64),
    config_json json,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS a2a_tasks (
    id character varying(64),
    tenant_id character varying(255),
    resource_group_id character varying(255),
    context_id character varying(64),
    state character varying(32),
    status_timestamp timestamp with time zone DEFAULT now(),
    task_json jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS action_records (
    record_id character varying(64),
    tenant_id character varying(255),
    log_id character varying(64),
    seq bigint,
    ai_system_id character varying(255),
    root_task_id character varying(255),
    proxy_log_id character varying(255),
    verdict character varying(32),
    kind character varying(8),
    surface character varying(32),
    epoch character varying(96),
    "timestamp" timestamp with time zone,
    tags jsonb,
    record jsonb,
    statement bytea,
    kid character varying(64),
    chain_parent_record_id character varying(64) GENERATED ALWAYS AS (((record -> 'chain'::text) ->> 'parent_record_id'::text)) STORED,
    chain_relation character varying(32) GENERATED ALWAYS AS (((record -> 'chain'::text) ->> 'relation'::text)) STORED,
    human_disposed boolean GENERATED ALWAYS AS (COALESCE((((record -> 'disposition'::text) -> 'human_disposed'::text) = 'true'::jsonb), false)) STORED,
    backfilled boolean GENERATED ALWAYS AS (COALESCE((((record -> 'context'::text) -> 'backfilled'::text) = 'true'::jsonb), false)) STORED,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_stream_tickets (
    ticket_hash character varying(64),
    stream character varying(64),
    admin_id character varying(255),
    admin_type character varying(32),
    tenant_id character varying(255),
    session_claims json,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_card_capabilities (
    id character varying(255),
    agent_card_id character varying(255),
    capability_name character varying(255),
    description text,
    input_schema json,
    output_schema json,
    timeout_secs integer,
    rate_limit_per_hour integer,
    data_classification json,
    handler_type character varying(32),
    handler_ref character varying(255),
    extensions jsonb,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_cards (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    version character varying(64) DEFAULT '1.0.0'::character varying,
    description text,
    source_url character varying(1024),
    icon_url character varying(2048),
    card_json json,
    supported_interfaces jsonb,
    signatures jsonb,
    extensions jsonb,
    protocol_version character varying(8) DEFAULT '0.3'::character varying,
    extended_card_json jsonb,
    tenant_limits json,
    tenant_governance json,
    signature text,
    signature_algorithm character varying(32),
    signed_by character varying(255),
    signed_at timestamp with time zone,
    trust_tier character varying(32) DEFAULT 'unsigned'::character varying,
    revoked_at timestamp with time zone,
    revoked_reason text,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    health_status character varying(32) DEFAULT 'unknown'::character varying,
    last_health_check_at timestamp with time zone,
    health_check_error text,
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS agent_grants (
    id character varying(255),
    tenant_id character varying(255),
    agent_id character varying(255),
    action_type character varying(16),
    target character varying(512) DEFAULT '*'::character varying,
    effect character varying(32),
    constraints jsonb,
    enabled boolean DEFAULT true,
    expires_at timestamp with time zone,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_identities (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description text,
    owner_user_id integer,
    owner_label character varying(255),
    idp_server_id character varying(255),
    idp_subject character varying(512),
    source character varying(16) DEFAULT 'manual'::character varying,
    enabled boolean DEFAULT true,
    revoked_at timestamp with time zone,
    expires_at timestamp with time zone,
    status character varying(16) DEFAULT 'active'::character varying,
    default_deny boolean DEFAULT true,
    enforcement_mode character varying(16) DEFAULT 'enforce'::character varying,
    autonomy_level character varying(24),
    implementation_name character varying(64),
    approved_versions jsonb DEFAULT '[]'::jsonb,
    require_release boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS agent_identity_approval_history (
    id character varying(64),
    tenant_id character varying(255),
    agent_id character varying(255),
    approved_versions jsonb,
    valid_from timestamp with time zone DEFAULT now(),
    valid_to timestamp with time zone,
    changed_by character varying(255),
    source character varying(32),
    CONSTRAINT ck_agent_identity_approval_history_period CHECK (((valid_to IS NULL) OR (valid_to >= valid_from))),
    CONSTRAINT ck_agent_identity_approval_history_source CHECK (((source)::text = ANY ((ARRAY['declaration'::character varying, 'backfill_contract'::character varying, 'backfill_live'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS agent_implementation_releases (
    id character varying(64),
    tenant_id character varying(255),
    agent_id character varying(255),
    name character varying(64),
    version character varying(64),
    build character varying(128) DEFAULT ''::character varying,
    trust character varying(16),
    first_seen_at timestamp with time zone,
    last_seen_at timestamp with time zone,
    run_count integer DEFAULT 0,
    fingerprints jsonb DEFAULT '[]'::jsonb,
    CONSTRAINT ck_agent_implementation_releases_trust CHECK (((trust)::text = ANY ((ARRAY['verified'::character varying, 'declared'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS agent_skill_assets (
    id character varying(255),
    tenant_id character varying(255),
    skill_id character varying(255),
    asset_type assettype,
    filename character varying(255),
    description text,
    content text,
    content_hash character varying(64),
    mime_type character varying(100),
    size_bytes integer,
    encoding character varying(20),
    vars_schema json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_skill_dependencies (
    id character varying(255),
    tenant_id character varying(255),
    parent_skill_id character varying(255),
    dependency_skill_id character varying(255),
    dependency_type dependencytype,
    execution_order integer,
    condition json,
    description character varying(500),
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS agent_skill_executions (
    id character varying(255),
    tenant_id character varying(255),
    skill_id character varying(255),
    group_id character varying(255),
    user_id integer,
    skill_name character varying(64),
    skill_version character varying(50),
    group_name character varying(255),
    username character varying(255),
    request_id character varying(255),
    correlation_id character varying(255),
    parent_execution_id character varying(255),
    execution_status executionstatus,
    started_at timestamp with time zone DEFAULT now(),
    running_at timestamp with time zone,
    completed_at timestamp with time zone,
    duration_ms integer,
    mcp_servers_used json,
    llm_models_used json,
    total_tokens integer,
    total_mcp_calls integer,
    estimated_cost_usd double precision,
    approval_status approvalstatus,
    approved_by character varying(255),
    approved_at timestamp with time zone,
    approval_notes text,
    approval_expires_at timestamp with time zone,
    error_type character varying(100),
    error_message text,
    error_details json,
    execution_context json,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_skill_resources (
    id character varying(255),
    tenant_id character varying(255),
    skill_id character varying(255),
    resource_type skillresourcetype,
    filename character varying(255),
    content text,
    content_hash character varying(64),
    mime_type character varying(100),
    size_bytes integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_skill_scripts (
    id character varying(255),
    tenant_id character varying(255),
    skill_id character varying(255),
    filename character varying(255),
    description text,
    script_content text,
    script_hash character varying(64),
    language scriptlanguage,
    execution_mode scriptexecutionmode,
    args_schema json,
    approved_by character varying(255),
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_skills (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(64),
    description character varying(1024),
    license character varying(100),
    compatibility json,
    allowed_tools json,
    tenant_limits json,
    tenant_governance json,
    skill_md_content text,
    skill_md_hash character varying(64),
    metadata json,
    version character varying(50),
    status skillstatus,
    enabled boolean,
    allow_local_export boolean DEFAULT false,
    disclosure_mode skilldisclosuremode DEFAULT 'PROGRESSIVE'::skilldisclosuremode,
    published_at timestamp with time zone,
    published_by character varying(255),
    deprecated_at timestamp with time zone,
    deprecation_reason text,
    validation_errors json,
    last_validated_at timestamp with time zone,
    execution_count integer,
    last_executed_at timestamp with time zone,
    workflow_definition json,
    workflow_executions json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    source_repo_id character varying(255),
    source_commit_sha character varying(64),
    source_path character varying(1024),
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS ai_asset_factsheets (
    id character varying(255),
    tenant_id character varying(255),
    asset_id character varying(255),
    period_hours integer,
    snapshot_json jsonb,
    trigger character varying(32),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_assets (
    id character varying(255),
    tenant_id character varying(255),
    asset_kind character varying(32),
    ref_id character varying(255),
    display_name character varying(255),
    icon_url character varying(1024),
    source_enabled boolean,
    observed boolean,
    owner character varying(255),
    lifecycle_stage character varying(32) DEFAULT 'active'::character varying,
    risk_tier character varying(32),
    intended_use text,
    gpai_classification character varying(24),
    operated_by character varying(16),
    enforcement_mode character varying(16) DEFAULT 'off'::character varying,
    tags jsonb,
    status character varying(32) DEFAULT 'ok'::character varying,
    source character varying(16) DEFAULT 'discovered'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_assurance_reports (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    period_hours integer,
    report_json jsonb,
    health_state character varying(16),
    contract_version integer,
    contract_hash character varying(80),
    trigger character varying(24) DEFAULT 'manual'::character varying,
    captured_by character varying(255),
    signature_json jsonb,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_baseline_epochs (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    metric_key character varying(64),
    basis character varying(16) DEFAULT 'runs'::character varying,
    caller_system_id character varying(255) DEFAULT 'all'::character varying,
    bucket character varying(128) DEFAULT 'all'::character varying,
    starts_at timestamp with time zone,
    kind character varying(32),
    drift_event_id character varying(64),
    measurement_change_id character varying(64),
    reason text,
    declared_by character varying(255),
    declared_at timestamp with time zone DEFAULT now(),
    record_id character varying(64),
    sealed_at timestamp with time zone,
    CONSTRAINT ck_ai_system_baseline_epochs_basis CHECK (((basis)::text = ANY ((ARRAY['runs'::character varying, 'segments'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_baseline_epochs_kind CHECK (((kind)::text = ANY ((ARRAY['accepted_change'::character varying, 'false_positive'::character varying, 'measurement_change'::character varying, 'catalogue_change'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_baselines (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    metric_key character varying(64),
    bucket character varying(128) DEFAULT 'all'::character varying,
    actor_id character varying(255) DEFAULT 'all'::character varying,
    basis character varying(16) DEFAULT 'runs'::character varying,
    caller_system_id character varying(255) DEFAULT 'all'::character varying,
    median double precision,
    mad double precision,
    p50 double precision,
    p90 double precision,
    p99 double precision,
    distribution jsonb,
    sample_count integer DEFAULT 0,
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    state character varying(16) DEFAULT 'learning'::character varying,
    computed_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_ai_system_baselines_basis CHECK (((basis)::text = ANY ((ARRAY['runs'::character varying, 'segments'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_compliance_profiles (
    ai_system_id character varying(255),
    tenant_id character varying(255),
    core jsonb DEFAULT '{}'::jsonb,
    frameworks jsonb DEFAULT '{}'::jsonb,
    declaration_record_id character varying(64),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS ai_system_contracts (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    version integer,
    contract_hash character varying(80),
    resolved_closure jsonb,
    status character varying(16) DEFAULT 'draft'::character varying,
    diff_from_version integer,
    diff_summary jsonb,
    lifecycle_stage_at_promotion character varying(32),
    dry_run_summary jsonb,
    replay_run_id character varying(64),
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    approved_by character varying(255),
    approved_at timestamp with time zone,
    promoted_at timestamp with time zone,
    rejection_reason text
);

CREATE TABLE IF NOT EXISTS ai_system_data_scopes (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    mode character varying(8) DEFAULT 'audit'::character varying,
    on_unbound character varying(8) DEFAULT 'deny'::character varying,
    keys jsonb DEFAULT '[]'::jsonb,
    rules jsonb DEFAULT '[]'::jsonb,
    unscoped_tools jsonb DEFAULT '[]'::jsonb,
    version integer DEFAULT 1,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by character varying(255),
    CONSTRAINT ck_data_scope_mode CHECK (((mode)::text = ANY ((ARRAY['off'::character varying, 'audit'::character varying, 'enforce'::character varying])::text[]))),
    CONSTRAINT ck_data_scope_on_unbound CHECK (((on_unbound)::text = ANY ((ARRAY['deny'::character varying, 'allow'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_dependencies (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    role character varying(32),
    depends_on_system_id character varying(255),
    agent_card_id character varying(255),
    criticality character varying(16) DEFAULT 'critical'::character varying,
    fallback text,
    contributes_to_decision boolean DEFAULT true,
    separability_rationale text,
    separability_record_id character varying(64),
    separability_sealed_at timestamp with time zone,
    contract_pin character varying(64),
    supplier_legal_entity character varying(255),
    assumptions jsonb DEFAULT '{}'::jsonb,
    expected_outcomes jsonb,
    declared_by character varying(255),
    declared_at timestamp with time zone DEFAULT now(),
    updated_by character varying(255),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_ai_system_dependencies_criticality CHECK (((criticality)::text = ANY ((ARRAY['critical'::character varying, 'degradable'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_dependencies_no_self_edge CHECK (((ai_system_id)::text <> (depends_on_system_id)::text)),
    CONSTRAINT ck_ai_system_dependencies_role CHECK (((role)::text = ANY ((ARRAY['shared_service'::character varying, 'external'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_drift_events (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    actor_id character varying(255),
    basis character varying(16) DEFAULT 'runs'::character varying,
    caller_system_id character varying(255),
    drift_class character varying(24),
    metric_key character varying(64),
    detected_at timestamp with time zone DEFAULT now(),
    change_point_at timestamp with time zone,
    baseline_value double precision,
    observed_value double precision,
    effect_size double precision,
    direction character varying(8),
    severity character varying(16) DEFAULT 'medium'::character varying,
    attributed_cause character varying(32) DEFAULT 'unexplained'::character varying,
    cause_confidence double precision,
    cause_evidence jsonb,
    correlated_metrics jsonb,
    response_taken jsonb,
    system_alert_id character varying(64),
    status character varying(16) DEFAULT 'active'::character varying,
    resolution_note text,
    resolved_by character varying(255),
    resolved_at timestamp with time zone,
    resolution_kind character varying(32),
    CONSTRAINT ck_ai_system_drift_events_basis CHECK (((basis)::text = ANY ((ARRAY['runs'::character varying, 'segments'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_drift_events_open_no_kind CHECK ((((status)::text <> ALL ((ARRAY['active'::character varying, 'acknowledged'::character varying])::text[])) OR (resolution_kind IS NULL))),
    CONSTRAINT ck_ai_system_drift_events_resolution_kind CHECK (((resolution_kind IS NULL) OR ((resolution_kind)::text = ANY ((ARRAY['accepted_change'::character varying, 'cause_fixed'::character varying, 'false_positive'::character varying, 'auto_cleared'::character varying])::text[]))))
);

CREATE TABLE IF NOT EXISTS ai_system_evidence (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    kind character varying(40),
    title character varying(500),
    notes text,
    uri text,
    sha256 character varying(64),
    assessor character varying(255),
    independent boolean,
    produced_at timestamp with time zone,
    valid_until timestamp with time zone,
    dependency_id character varying(64),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS ai_system_health (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    health_state character varying(16) DEFAULT 'unknown'::character varying,
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
    runs_24h integer DEFAULT 0,
    last_run_at timestamp with time zone,
    open_drift_events integer DEFAULT 0,
    reasons jsonb,
    dependency_cap jsonb,
    recent_runs jsonb,
    computed_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_incident_window_findings (
    incident_window_id character varying(64),
    drift_event_id character varying(64),
    tenant_id character varying(255),
    role character varying(16),
    attached_by character varying(255),
    attached_at timestamp with time zone DEFAULT now(),
    record_id character varying(64),
    sealed_at timestamp with time zone,
    CONSTRAINT ck_ai_system_incident_window_findings_role CHECK (((role)::text = ANY ((ARRAY['declared'::character varying, 'attached'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_incident_windows (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    resource_type character varying(32),
    resource_id character varying(255),
    starts_at timestamp with time zone,
    ends_at timestamp with time zone,
    cause_category character varying(32),
    reason text,
    drift_event_id character varying(64),
    declared_by character varying(255),
    declared_at timestamp with time zone DEFAULT now(),
    updated_by character varying(255),
    updated_at timestamp with time zone,
    record_id character varying(64),
    sealed_at timestamp with time zone,
    CONSTRAINT ck_ai_system_incident_windows_bounds CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_ai_system_incident_windows_cause CHECK (((cause_category)::text = ANY ((ARRAY['provider_outage'::character varying, 'platform_incident'::character varying, 'maintenance'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_incident_windows_resource CHECK (((resource_type IS NULL) = (resource_id IS NULL))),
    CONSTRAINT ck_ai_system_incident_windows_resource_type CHECK (((resource_type IS NULL) OR ((resource_type)::text = ANY ((ARRAY['model'::character varying, 'provider'::character varying, 'mcp_server'::character varying, 'skill'::character varying, 'knowledge_base'::character varying, 'agent'::character varying])::text[]))))
);

CREATE TABLE IF NOT EXISTS ai_system_lifecycle_events (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    event_type character varying(32) DEFAULT 'stage_transition'::character varying,
    from_stage character varying(32),
    to_stage character varying(32),
    subject_id character varying(64),
    details jsonb,
    approved_by character varying(255),
    recorded_by character varying(255),
    recorded_by_id character varying(255),
    successor_system_id character varying(255),
    no_successor boolean DEFAULT false,
    note text,
    created_at timestamp with time zone,
    CONSTRAINT ck_ai_system_lifecycle_events_stage CHECK ((((event_type)::text <> 'stage_transition'::text) OR (to_stage IS NOT NULL))),
    CONSTRAINT ck_ai_system_lifecycle_events_type CHECK (((event_type)::text = ANY ((ARRAY['stage_transition'::character varying, 'agent.new_release'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_liveness_expectations (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    mode character varying(16) DEFAULT 'on_demand'::character varying,
    expected_min_runs integer DEFAULT 1,
    window_seconds integer DEFAULT 3600,
    grace_seconds integer DEFAULT 300,
    enabled boolean DEFAULT true,
    max_quiet_processed_seconds integer,
    learned_from_runs integer,
    learned_median_gap_seconds integer,
    learned_p95_gap_seconds integer,
    learned_at timestamp with time zone,
    accepted_from_learned boolean DEFAULT false,
    last_seen_run_at timestamp with time zone,
    last_checked_at timestamp with time zone,
    last_alert_at timestamp with time zone,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_reach_rules (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    scope_key character varying(64),
    group_by character varying(72),
    window_hours integer DEFAULT 24,
    max_distinct integer,
    severity character varying(8) DEFAULT 'high'::character varying,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    CONSTRAINT ck_reach_max CHECK ((max_distinct >= 1)),
    CONSTRAINT ck_reach_severity CHECK (((severity)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT ck_reach_window CHECK (((window_hours >= 1) AND (window_hours <= 720)))
);

CREATE TABLE IF NOT EXISTS ai_system_record_access (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    root_task_id character varying(255),
    correlation_id character varying(255),
    tool character varying(255),
    scope_key character varying(64),
    record_ref character varying(64),
    verdict character varying(20),
    mode character varying(8),
    subject_id character varying(255),
    api_key_id character varying(255),
    agent_id character varying(255),
    accessed_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_replay_cases (
    id character varying(64),
    tenant_id character varying(255),
    suite_id character varying(64),
    source_run_id character varying(64),
    source_root_task_id character varying(255),
    recorded_request jsonb,
    recorded_trajectory jsonb,
    recorded_terminal_state character varying(32),
    recorded_action_count integer,
    recorded_tool_signature character varying(512),
    stratum character varying(64),
    side_effect_policy character varying(16) DEFAULT 'mock'::character varying,
    criticality character varying(16) DEFAULT 'normal'::character varying,
    enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_replay_runs (
    id character varying(64),
    tenant_id character varying(255),
    suite_id character varying(64),
    ai_system_id character varying(255),
    contract_id character varying(64),
    status character varying(16) DEFAULT 'running'::character varying,
    triggered_by character varying(24) DEFAULT 'manual'::character varying,
    triggered_by_actor character varying(255),
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone,
    total_cases integer DEFAULT 0,
    matched integer DEFAULT 0,
    shape_changed integer DEFAULT 0,
    policy_changed integer DEFAULT 0,
    outcome_changed integer DEFAULT 0,
    failed integer DEFAULT 0,
    skipped integer DEFAULT 0,
    pass_rate double precision,
    critical_outcome_changed integer DEFAULT 0,
    case_results jsonb,
    error_message text
);

CREATE TABLE IF NOT EXISTS ai_system_replay_suites (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    name character varying(255),
    description text,
    pass_threshold double precision DEFAULT '0.9'::double precision,
    sampling_strategy character varying(16) DEFAULT 'stratified'::character varying,
    target_case_count integer DEFAULT 30,
    replayable boolean DEFAULT true,
    not_replayable_reason text,
    enabled boolean DEFAULT true,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_response_policies (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    name character varying(255),
    description text,
    enabled boolean DEFAULT true,
    signal_kind character varying(16) DEFAULT 'drift'::character varying,
    drift_class character varying(24),
    severity_at_least character varying(16) DEFAULT 'high'::character varying,
    actions jsonb,
    allow_suspend boolean DEFAULT false,
    require_human_to_restore boolean DEFAULT true,
    response_cooldown_seconds integer DEFAULT 3600,
    last_fired_at timestamp with time zone,
    last_fired_drift_event_id character varying(64),
    fire_count integer DEFAULT 0,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_run_feedback (
    id character varying(64),
    tenant_id character varying(255),
    root_task_id character varying(255),
    user_id character varying(255),
    score smallint,
    comment text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_run_scopes (
    id character varying(64),
    tenant_id character varying(255),
    root_task_id character varying(255),
    scope_key character varying(64),
    scope_values jsonb,
    source character varying(16) DEFAULT 'declared'::character varying,
    declared_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_system_run_segments (
    id character varying(64),
    run_id character varying(64),
    tenant_id character varying(255),
    root_task_id character varying(255),
    root_system_id character varying(255),
    segment_system_id character varying(255),
    caller_system_id character varying(255),
    min_depth smallint DEFAULT '0'::smallint,
    first_action_at timestamp with time zone,
    last_action_at timestamp with time zone,
    active_ms integer DEFAULT 0,
    action_count integer DEFAULT 0,
    llm_call_count integer DEFAULT 0,
    tool_call_count integer DEFAULT 0,
    error_count integer DEFAULT 0,
    tool_error_count integer DEFAULT 0,
    guardrail_block_count integer DEFAULT 0,
    policy_denial_count integer DEFAULT 0,
    approval_request_count integer DEFAULT 0,
    total_tokens bigint DEFAULT '0'::bigint,
    cost_usd numeric(18,8) DEFAULT '0'::numeric,
    models_used jsonb DEFAULT '{}'::jsonb,
    tool_set_signature character varying(64),
    caller_agent_id character varying(255),
    a2a_task_state character varying(32),
    outcome character varying(16),
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
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_ai_system_run_segments_agent_impl_trust CHECK (((agent_impl_trust)::text = ANY ((ARRAY['verified'::character varying, 'declared'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_run_segments_outcome CHECK (((outcome)::text = ANY ((ARRAY['answered'::character varying, 'failed'::character varying, 'rejected'::character varying, 'timed_out'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS ai_system_runs (
    id character varying(64),
    tenant_id character varying(255),
    root_task_id character varying(255),
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
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    duration_ms integer,
    awaiting_human_ms integer DEFAULT 0,
    active_duration_ms integer DEFAULT 0,
    terminal_state character varying(24),
    finalized_by character varying(16),
    action_count integer DEFAULT 0,
    llm_call_count integer DEFAULT 0,
    tool_call_count integer DEFAULT 0,
    skill_call_count integer DEFAULT 0,
    a2a_call_count integer DEFAULT 0,
    turn_count integer DEFAULT 0,
    turn_attribution character varying(16) DEFAULT 'inferred'::character varying,
    step_count integer DEFAULT 0,
    max_delegation_depth integer DEFAULT 0,
    distinct_tools integer DEFAULT 0,
    tools_used jsonb,
    scope_declared boolean,
    records_touched integer,
    scope_violation_count integer,
    scope_unbound_count integer,
    tool_signature character varying(64),
    tool_set_signature character varying(64),
    total_tokens integer DEFAULT 0,
    prompt_tokens integer DEFAULT 0,
    completion_tokens integer DEFAULT 0,
    total_cost_usd numeric(18,8) DEFAULT '0'::numeric,
    models_used jsonb,
    cache_hit_count integer DEFAULT 0,
    tokens_saved integer DEFAULT 0,
    error_count integer DEFAULT 0,
    retry_count integer DEFAULT 0,
    fallback_count integer DEFAULT 0,
    tool_error_count integer DEFAULT 0,
    tool_error_types jsonb,
    guardrail_trip_count integer DEFAULT 0,
    guardrail_block_count integer DEFAULT 0,
    policy_denial_count integer DEFAULT 0,
    approval_request_count integer DEFAULT 0,
    input_bytes integer DEFAULT 0,
    output_bytes integer DEFAULT 0,
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
    rag_used boolean DEFAULT false,
    rag_chunks integer DEFAULT 0,
    prompt_fingerprint character varying(64),
    rag_collection_ids character varying(1024),
    outcome_id character varying(128),
    outcome_conformance character varying(16),
    outcome_counts_toward_baseline boolean,
    outcome_contract_version integer,
    error_cause character varying(32),
    chain_integrity character varying(24) DEFAULT 'intact'::character varying,
    reopened_count integer DEFAULT 0,
    source character varying(16) DEFAULT 'gateway'::character varying,
    is_replay boolean DEFAULT false,
    feedback_score smallint,
    created_at timestamp with time zone DEFAULT now(),
    credential_assurance_min character varying(16),
    credential_mixed boolean,
    credential_refs jsonb,
    CONSTRAINT ck_ai_system_runs_agent_impl_trust CHECK (((agent_impl_trust)::text = ANY ((ARRAY['verified'::character varying, 'declared'::character varying])::text[]))),
    CONSTRAINT ck_ai_system_runs_credential_assurance_min CHECK (((credential_assurance_min IS NULL) OR ((credential_assurance_min)::text = ANY ((ARRAY['key'::character varying, 'bound_key'::character varying, 'workload'::character varying, 'workload_pop'::character varying])::text[]))))
);

CREATE TABLE IF NOT EXISTS api_keys (
    id character varying(255),
    tenant_id character varying(255),
    key_hash character varying(255),
    key_prefix character varying(50),
    name character varying(200),
    description text,
    scope character varying(20),
    allowed_skill_ids json,
    skill_rate_limit_daily integer,
    user_id integer,
    user_email character varying(255),
    agent_id character varying(255),
    group_id character varying(255),
    expires_at timestamp with time zone,
    enabled boolean,
    is_platform_service boolean DEFAULT false,
    revoked boolean,
    revoked_at timestamp with time zone,
    revoked_by character varying(255),
    revoked_reason text,
    last_used_at timestamp with time zone,
    last_used_by_user_id integer,
    usage_count integer,
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_at timestamp with time zone DEFAULT now(),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS argument_policies (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description character varying(1024),
    surface character varying(64),
    target character varying(255),
    argument_key character varying(255),
    analyzer character varying(32),
    analyzer_config jsonb,
    rules jsonb DEFAULT '[]'::jsonb,
    enabled boolean DEFAULT true,
    severity character varying(32) DEFAULT 'deny'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_argument_policies_analyzer CHECK (((analyzer)::text = ANY ((ARRAY['sql'::character varying, 'url'::character varying, 'shell'::character varying, 'path'::character varying, 'json'::character varying])::text[]))),
    CONSTRAINT ck_argument_policies_severity CHECK (((severity)::text = ANY ((ARRAY['deny'::character varying, 'warn'::character varying, 'approval_required'::character varying])::text[]))),
    CONSTRAINT ck_argument_policies_surface CHECK (((surface)::text = ANY ((ARRAY['llm_output_tool_calls'::character varying, 'mcp_input'::character varying, 'skill_input'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS assurance_check_results (
    id character varying(64),
    tenant_id character varying(255),
    check_id character varying(64),
    check_version integer,
    run_id character varying(64),
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    value jsonb,
    evidence jsonb DEFAULT '{}'::jsonb,
    evaluator_fingerprint character varying(64),
    backtest boolean DEFAULT false,
    tokens_used integer,
    evaluated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assurance_check_reviews (
    id character varying(64),
    tenant_id character varying(255),
    check_id character varying(64),
    evaluator_fingerprint character varying(64),
    week date,
    sample_key character varying(255),
    assessment_record_id character varying(64),
    judged_result character varying(16),
    judged_score numeric(10,4),
    human_result character varying(16),
    human_score numeric(10,4),
    status character varying(16) DEFAULT 'pending'::character varying,
    blind boolean DEFAULT true,
    reviewer_id character varying(255),
    reviewed_at timestamp with time zone,
    adjudication_record_id character varying(64),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assurance_check_rubrics (
    id character varying(64),
    tenant_id character varying(255),
    check_id character varying(64),
    rubric_digest character varying(64),
    rubric jsonb,
    period_start date,
    period_end date,
    record_id character varying(64),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assurance_check_versions (
    id character varying(64),
    tenant_id character varying(255),
    check_id character varying(64),
    version integer,
    spec_hash character varying(64),
    spec jsonb,
    backtest_summary jsonb,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assurance_checks (
    id character varying(64),
    tenant_id character varying(255),
    name character varying(255),
    description_nl text,
    version integer DEFAULT 1,
    spec_hash character varying(64),
    scope jsonb DEFAULT '{}'::jsonb,
    tier character varying(16),
    evaluator jsonb,
    output character varying(24),
    trigger character varying(16),
    threshold jsonb,
    route_to_inbox boolean DEFAULT true,
    enabled boolean DEFAULT false,
    created_by character varying(255),
    approved_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_achk_output CHECK (((output)::text = ANY ((ARRAY['boolean'::character varying, 'classification'::character varying, 'score'::character varying, 'count'::character varying, 'extraction'::character varying])::text[]))),
    CONSTRAINT ck_achk_tier CHECK (((tier)::text = ANY ((ARRAY['deterministic'::character varying, 'semantic'::character varying])::text[]))),
    CONSTRAINT ck_achk_trigger CHECK (((trigger)::text = ANY ((ARRAY['on_finalize'::character varying, 'batch'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS assurance_digests (
    id character varying(64),
    tenant_id character varying(255),
    week_start date,
    content jsonb,
    generated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assurance_inbox_events (
    id character varying(64),
    item_id character varying(64),
    tenant_id character varying(255),
    at timestamp with time zone DEFAULT now(),
    admin character varying(255),
    event character varying(32),
    detail jsonb,
    CONSTRAINT ck_inbox_event CHECK (((event)::text = ANY ((ARRAY['created'::character varying, 'reoccurred'::character varying, 'acknowledged'::character varying, 'assigned'::character varying, 'resolved'::character varying, 'reopened'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS assurance_inbox_items (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    category character varying(32),
    severity character varying(16),
    fingerprint character varying(512),
    title character varying(500),
    evidence jsonb DEFAULT '{}'::jsonb,
    occurrence_count integer DEFAULT 1,
    first_seen_at timestamp with time zone DEFAULT now(),
    last_seen_at timestamp with time zone DEFAULT now(),
    state character varying(16) DEFAULT 'open'::character varying,
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

CREATE TABLE IF NOT EXISTS audit_chain_checkpoints (
    id character varying(64),
    chain_id character varying(64),
    seq_start bigint,
    seq_end bigint,
    head_hash character varying(64),
    signature character varying(128),
    public_key character varying(64),
    signed_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_chain_prunes (
    id character varying(64),
    chain_id character varying(64),
    tenant_id character varying(255),
    seq_min bigint,
    seq_max bigint,
    row_count integer,
    runs jsonb,
    reason character varying(16),
    actor character varying(255),
    pruned_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_export_destinations (
    id character varying(64),
    tenant_id character varying(255),
    name character varying(255),
    destination_type character varying(32),
    enabled boolean,
    config json,
    field_allowlist json,
    interval_seconds integer,
    last_exported_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_run_status character varying(32),
    last_run_rows integer,
    last_run_error text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id character varying(255),
    tenant_id character varying(255),
    resource_group_id character varying(255),
    resource_group_name character varying(255),
    user_id integer,
    username character varying(255),
    api_key_id character varying(255),
    agent_id character varying(255),
    llm_model_id character varying(255),
    llm_model_name character varying(255),
    provider character varying(100),
    model character varying(255),
    endpoint character varying(255),
    completion_window character varying(50),
    provider_batch_id character varying(500),
    provider_input_file_id character varying(500),
    provider_output_file_id character varying(500),
    provider_error_file_id character varying(500),
    description text,
    custom_metadata json,
    status character varying(50),
    created_at timestamp with time zone DEFAULT now(),
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
    total_requests integer,
    completed_requests integer,
    failed_requests integer,
    total_input_tokens integer,
    total_output_tokens integer,
    estimated_cost_usd double precision,
    error_type character varying(100),
    error_message text,
    error_details json
);

CREATE TABLE IF NOT EXISTS batch_requests (
    id character varying(255),
    tenant_id character varying(255),
    batch_job_id character varying(255),
    custom_id character varying(500),
    request_index integer,
    status character varying(50),
    request_body json,
    model_requested character varying(255),
    response_status_code integer,
    response_body json,
    input_tokens integer,
    output_tokens integer,
    estimated_cost_usd double precision,
    error_type character varying(100),
    error_message text,
    created_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS collector_keys (
    key_id character varying(255),
    tenant_id character varying(255),
    collector_id character varying(255),
    collector_type character varying(64),
    public_key text,
    trust_tier character varying(16) DEFAULT 'customer'::character varying,
    enabled boolean DEFAULT true,
    revoked_at timestamp with time zone,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    last_used_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS compliance_documentation_exports (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    framework_id character varying(64),
    template character varying(64),
    contract_version integer,
    content jsonb,
    digest character varying(64),
    record_id character varying(64),
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compliance_frameworks (
    id character varying(64),
    tag_key character varying(64),
    title character varying(255),
    kind character varying(16),
    jurisdiction character varying(64),
    reference text,
    catalog_version character varying(32) DEFAULT '2026-09'::character varying,
    verified_against_instrument boolean DEFAULT false,
    retention_floor_days integer,
    deadline_rules jsonb DEFAULT '{}'::jsonb,
    sort_order integer DEFAULT 100,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compliance_incident_events (
    id character varying(64),
    incident_id character varying(64),
    tenant_id character varying(255),
    event character varying(16),
    at timestamp with time zone DEFAULT now(),
    actor_id character varying(255),
    detail jsonb DEFAULT '{}'::jsonb,
    record_id character varying(64)
);

CREATE TABLE IF NOT EXISTS compliance_incidents (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    title character varying(255),
    description text DEFAULT ''::text,
    classification character varying(64),
    severity character varying(16),
    became_aware_at timestamp with time zone,
    classified_at timestamp with time zone,
    frameworks jsonb DEFAULT '[]'::jsonb,
    deadlines jsonb DEFAULT '{}'::jsonb,
    status character varying(16) DEFAULT 'open'::character varying,
    reported_at timestamp with time zone,
    authority character varying(255),
    external_reference character varying(255),
    linked_run_ids jsonb DEFAULT '[]'::jsonb,
    linked_inbox_item_ids jsonb DEFAULT '[]'::jsonb,
    stage_submissions jsonb DEFAULT '{}'::jsonb,
    last_record_id character varying(64),
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compliance_notice_configs (
    id character varying(64),
    tenant_id character varying(255),
    ai_system_id character varying(255),
    enabled boolean DEFAULT true,
    surface character varying(16) DEFAULT 'both'::character varying,
    texts jsonb DEFAULT '{}'::jsonb,
    text_digests jsonb DEFAULT '{}'::jsonb,
    exemption jsonb,
    record_id character varying(64),
    updated_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compliance_reports (
    id character varying(64),
    tenant_id character varying(255),
    framework_id character varying(64),
    ai_system_id character varying(255),
    cadence character varying(16),
    period_start date,
    period_end date,
    catalog_version character varying(32),
    status character varying(16) DEFAULT 'generating'::character varying,
    summary jsonb DEFAULT '{}'::jsonb,
    rows jsonb DEFAULT '[]'::jsonb,
    error text,
    record_id character varying(64),
    bundle_id character varying(64),
    countersignatures jsonb DEFAULT '[]'::jsonb,
    generated_by character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS data_erasure_events (
    id character varying(64),
    tenant_id character varying(255),
    subject_fingerprint character varying(64),
    subject_kinds jsonb,
    requested_by character varying(255),
    requested_at timestamp with time zone DEFAULT now(),
    dry_run boolean DEFAULT false,
    counts jsonb
);

CREATE TABLE IF NOT EXISTS disclosure_records (
    id character varying(64),
    tenant_id character varying(255),
    log_id character varying(64),
    seq bigint,
    audience character varying(255),
    selector jsonb DEFAULT '{}'::jsonb,
    completeness character varying(24),
    disclosed_record_ids jsonb DEFAULT '[]'::jsonb,
    suppressed_fields jsonb DEFAULT '[]'::jsonb,
    bundle_digest character varying(64),
    statement bytea,
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS discovered_assets (
    id character varying(255),
    tenant_id character varying(255),
    asset_key character varying(512),
    asset_type character varying(32),
    provider character varying(64),
    display_name character varying(512),
    model_hint character varying(255),
    endpoint_scheme character varying(16),
    endpoint_host character varying(255),
    endpoint_port integer,
    endpoint_path character varying(512),
    computed_confidence double precision DEFAULT '0'::double precision,
    governance_status character varying(16) DEFAULT 'unknown'::character varying,
    governed_kind character varying(32),
    governed_ref character varying(255),
    device_id character varying(128),
    device_name character varying(255),
    status character varying(16) DEFAULT 'active'::character varying,
    evidence_count integer DEFAULT 0,
    first_seen timestamp with time zone DEFAULT now(),
    last_seen timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS discovery_events (
    event_id character varying(255),
    tenant_id character varying(255),
    collector_id character varying(255),
    collector_type character varying(64),
    collector_version character varying(64),
    trust_tier character varying(16),
    asset_key character varying(512),
    emitted_at timestamp with time zone,
    received_at timestamp with time zone DEFAULT now(),
    accepted boolean DEFAULT true,
    reject_reason character varying(255),
    payload jsonb
);

CREATE TABLE IF NOT EXISTS discovery_evidence (
    id character varying(255),
    tenant_id character varying(255),
    discovered_asset_id character varying(255),
    evidence_type character varying(32),
    observed_at timestamp with time zone,
    observation text,
    passive boolean DEFAULT false,
    host_derived boolean DEFAULT false,
    collector_id character varying(255),
    trust_tier character varying(16),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS docker_deployments (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    deployment_name character varying(255),
    mcp_server_id character varying(255),
    mcp_server_name character varying(255),
    image_id integer,
    image_name character varying(600),
    container_id character varying(64),
    container_name character varying(255),
    github_repo character varying(500),
    network character varying(255),
    host_port integer,
    internal_port integer,
    environment json,
    volumes json,
    memory_limit character varying(50),
    cpu_limit double precision,
    restart_policy character varying(50),
    deployed_by character varying(50),
    deployed_at timestamp without time zone DEFAULT now(),
    status character varying(50),
    last_status_check timestamp without time zone,
    last_started_at timestamp without time zone,
    last_stopped_at timestamp without time zone,
    health_status character varying(50),
    consecutive_failures integer,
    cpu_usage_percent double precision,
    memory_usage_mb double precision,
    network_rx_mb double precision,
    network_tx_mb double precision,
    stats_updated_at timestamp without time zone
);

CREATE TABLE IF NOT EXISTS docker_images (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    image_id character varying(64),
    repository character varying(500),
    tag character varying(100),
    full_name character varying(600),
    size_bytes integer,
    created_at timestamp without time zone,
    build_method character varying(50),
    dockerfile_content text,
    build_args json,
    build_logs text,
    github_repo character varying(500),
    github_branch character varying(100),
    build_path character varying(500),
    managed_by character varying(50),
    imported_at timestamp without time zone DEFAULT now(),
    last_used timestamp without time zone,
    is_active boolean
);

CREATE TABLE IF NOT EXISTS ecs_deployments (
    id character varying(255),
    tenant_id character varying(255),
    deployment_name character varying(255),
    mcp_server_id character varying(255),
    mcp_server_name character varying(255),
    cluster_name character varying(255),
    task_definition_arn character varying(512),
    task_arn character varying(512),
    region character varying(50),
    eni_id character varying(255),
    private_ip character varying(45),
    image_name character varying(512),
    container_port integer DEFAULT 8000,
    status character varying(50) DEFAULT 'pending'::character varying,
    last_status_check timestamp with time zone,
    deployed_at timestamp with time zone,
    stopped_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS edition_marker (
    id integer,
    enterprise_first_started_at timestamp with time zone,
    enterprise_last_started_at timestamp with time zone,
    enterprise_first_version character varying(50),
    enterprise_last_version character varying(50),
    CONSTRAINT ck_edition_marker_single_row CHECK ((id = 1))
);

CREATE TABLE IF NOT EXISTS end_user_group_members (
    end_user_group_id character varying(64),
    end_user_id integer,
    created_at timestamp with time zone DEFAULT now(),
    source character varying(16) DEFAULT 'manual'::character varying
);

CREATE TABLE IF NOT EXISTS end_user_groups (
    id character varying(64),
    tenant_id character varying(255),
    name character varying(255),
    display_name character varying(255),
    description text,
    source character varying(16) DEFAULT 'manual'::character varying,
    source_external_id character varying(255),
    idp_server_id character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS end_user_revoked_tokens (
    jti character varying(64),
    tenant_id character varying(255),
    end_user_id integer,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone DEFAULT now(),
    reason character varying(32) DEFAULT 'logout'::character varying
);

CREATE TABLE IF NOT EXISTS end_users (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    role_id character varying(50),
    primary_group_id character varying(255),
    username character varying(100),
    email character varying(255),
    display_name character varying(255),
    password_hash character varying(255),
    is_verified boolean,
    is_active boolean,
    created_at timestamp with time zone DEFAULT now(),
    last_login timestamp with time zone,
    user_metadata json,
    avatar_url character varying(500),
    photo_data bytea,
    photo_mime_type character varying(100),
    photo_size integer,
    photo_hash character varying(64),
    totp_secret text,
    totp_enabled boolean,
    totp_verified_at timestamp with time zone,
    backup_codes json,
    sessions_valid_after timestamp with time zone,
    idp_server_id character varying(255),
    idp_subject character varying(500),
    idp_last_login timestamp with time zone,
    source character varying(16) DEFAULT 'manual'::character varying,
    source_external_id character varying(255)
);

CREATE TABLE IF NOT EXISTS evidence_bundles (
    id character varying(64),
    tenant_id character varying(255),
    log_id character varying(64),
    selector jsonb,
    audience character varying(255),
    payloads_mode character varying(16),
    selection character varying(24),
    framework_id character varying(64),
    catalog_version character varying(32),
    obligation_key character varying(128),
    root_record_id character varying(64),
    record_count integer,
    bundle_digest character varying(64),
    record_id character varying(64),
    disclosure_record_id character varying(64),
    bundle jsonb,
    size_bytes integer,
    requested_by character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence_log_entries (
    log_id character varying(64),
    seq bigint,
    kind character varying(24),
    leaf_hash character varying(64),
    ref_id character varying(64),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence_log_nodes (
    log_id character varying(64),
    level integer,
    index bigint,
    hash character varying(64)
);

CREATE TABLE IF NOT EXISTS evidence_logs (
    log_id character varying(64),
    tenant_id character varying(255),
    next_seq bigint DEFAULT '0'::bigint,
    tree_size bigint DEFAULT '0'::bigint,
    root character varying(64),
    kid character varying(64),
    last_tree_head_id character varying(64),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence_receipts (
    id character varying(64),
    tree_head_id character varying(64),
    witness_id character varying(64),
    kind character varying(16),
    receipt bytea,
    leaf_index bigint,
    tree_size bigint,
    witness_iat bigint,
    verdict character varying(16) DEFAULT 'unverified'::character varying,
    verified_at timestamp with time zone,
    vendor_ext jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence_settings (
    tenant_id character varying(255),
    per_tenant_key boolean DEFAULT false,
    salt_digests_default boolean DEFAULT false,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS evidence_signing_keys (
    kid character varying(64),
    tenant_id character varying(255),
    encrypted_seed text,
    status character varying(16) DEFAULT 'active'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    retired_at timestamp with time zone,
    rotation_record_id character varying(64)
);

CREATE TABLE IF NOT EXISTS evidence_tree_heads (
    id character varying(64),
    log_id character varying(64),
    tenant_id character varying(255),
    tree_size bigint,
    root character varying(64),
    prev_size bigint DEFAULT '0'::bigint,
    prev_root character varying(64),
    statement bytea,
    transparent_statement bytea,
    issued_at timestamp with time zone,
    stamp_seq bigint,
    grade character varying(16) DEFAULT 'standalone'::character varying,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS guardrail_break_glass (
    id character varying(255),
    tenant_id character varying(255),
    reason text,
    activated_by character varying(255),
    activated_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone,
    deactivated_at timestamp with time zone,
    deactivated_by character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS guardrail_configs (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description character varying(1024),
    is_default boolean DEFAULT false,
    priority integer,
    scope_mcp_server_id character varying(255),
    scope_tool_name character varying(255),
    enabled boolean DEFAULT false,
    chat_input_enabled boolean DEFAULT true,
    chat_output_enabled boolean DEFAULT false,
    streaming_output_mode character varying(10) DEFAULT 'auto'::character varying,
    embeddings_input_enabled boolean DEFAULT true,
    image_gen_input_enabled boolean DEFAULT true,
    image_gen_output_enabled boolean DEFAULT false,
    audio_tts_input_enabled boolean DEFAULT true,
    audio_tts_output_enabled boolean DEFAULT false,
    audio_stt_input_enabled boolean DEFAULT true,
    audio_stt_output_enabled boolean DEFAULT true,
    video_gen_input_enabled boolean DEFAULT true,
    video_gen_output_enabled boolean DEFAULT false,
    batch_input_enabled boolean DEFAULT true,
    batch_output_enabled boolean DEFAULT false,
    moderation_input_enabled boolean DEFAULT true,
    mcp_input_enabled boolean DEFAULT true,
    mcp_output_enabled boolean DEFAULT true,
    mcp_registration_enabled boolean DEFAULT true,
    skill_input_enabled boolean DEFAULT true,
    skill_output_enabled boolean DEFAULT true,
    a2a_inbound_enabled boolean DEFAULT false,
    a2a_outbound_enabled boolean DEFAULT false,
    pii_detection_enabled boolean DEFAULT true,
    pii_action character varying(20),
    pii_provider character varying(32),
    pii_categories json,
    pii_threshold double precision,
    prompt_injection_enabled boolean DEFAULT true,
    prompt_injection_action character varying(20),
    prompt_injection_sensitivity double precision,
    prompt_injection_provider character varying(32),
    jailbreak_detection_enabled boolean DEFAULT true,
    jailbreak_action character varying(20),
    jailbreak_provider character varying(32),
    toxic_content_enabled boolean DEFAULT false,
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
    secrets_detection_enabled boolean DEFAULT false,
    secrets_action character varying(20) DEFAULT 'redact'::character varying,
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
    fail_open boolean DEFAULT false,
    log_all_checks boolean,
    fail_open_overrides json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS health_check_configs (
    id character varying(255),
    tenant_id character varying(255),
    dashboard_enabled boolean,
    dashboard_interval_seconds integer,
    dashboard_show_ai_models boolean,
    dashboard_show_mcp_servers boolean,
    mcp_enabled boolean,
    mcp_interval_seconds integer,
    mcp_timeout_seconds integer,
    mcp_retry_attempts integer,
    mcp_failure_threshold integer,
    mcp_pause_when_inactive boolean,
    mcp_recovery_check_interval integer,
    ai_enabled boolean,
    ai_interval_seconds integer,
    ai_timeout_seconds integer,
    ai_retry_attempts integer,
    ai_failure_threshold integer,
    ai_pause_when_inactive boolean,
    ai_recovery_check_interval integer,
    ai_test_prompt character varying(500),
    ai_max_tokens integer,
    created_at timestamp with time zone DEFAULT now(),
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

CREATE TABLE IF NOT EXISTS idp_servers (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    issuer character varying(500),
    type character varying(50),
    well_known_url character varying(500),
    authorization_endpoint character varying(500),
    token_endpoint character varying(500),
    revocation_endpoint character varying(500),
    introspection_endpoint character varying(500),
    jwks_uri character varying(500),
    userinfo_endpoint character varying(500),
    registration_endpoint character varying(500),
    supports_pkce boolean,
    supports_refresh_tokens boolean,
    supports_client_credentials boolean,
    token_endpoint_auth_methods json,
    scopes_supported json,
    response_types_supported json,
    grant_types_supported json,
    cached_config json,
    last_discovered timestamp with time zone,
    discovery_cache_ttl integer,
    enabled boolean,
    verified boolean,
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
    trust_saml_email_unverified boolean DEFAULT false,
    saml_last_metadata_refresh timestamp with time zone,
    workload_audiences json,
    target_user_type character varying(20),
    saml_group_to_end_user_group_mapping json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS iso42001_control_coverage (
    id character varying(255),
    tenant_id character varying(255),
    control_id character varying(16),
    status character varying(32) DEFAULT 'not_implemented'::character varying,
    owner character varying(255),
    evidence jsonb,
    notes text,
    last_reviewed timestamp with time zone,
    next_review timestamp with time zone,
    updated_by character varying(255),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS iso42001_controls (
    control_id character varying(16),
    clause character varying(16),
    category character varying(255),
    title character varying(255),
    summary text,
    brutor_note text,
    brutor_capability character varying(16) DEFAULT 'org'::character varying,
    sort_order integer DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id character varying(255),
    document_id character varying(255),
    tenant_id character varying(255),
    chunk_index integer,
    content text,
    token_count integer,
    qdrant_point_id character varying(255),
    metadata_json json,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_collections (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description character varying(1024),
    enabled boolean,
    top_k integer,
    similarity_threshold double precision,
    max_context_tokens integer,
    chunk_size_tokens integer DEFAULT 500,
    chunk_overlap_tokens integer DEFAULT 50,
    system_prompt_template text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying
);

CREATE TABLE IF NOT EXISTS kb_connector_checkpoints (
    connector_id character varying(255),
    cursor json DEFAULT '{}'::json,
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_connector_credentials (
    id character varying(255),
    connector_id character varying(255),
    credential_kind character varying(32),
    encrypted_payload text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_kb_connector_credentials_kind CHECK (((credential_kind)::text = ANY ((ARRAY['api_key'::character varying, 'oauth'::character varying, 'service_account'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS kb_connector_identity_mappings (
    id character varying(255),
    tenant_id character varying(255),
    end_user_id integer,
    connector_type character varying(64),
    source_principal character varying(512),
    mapping_source character varying(32) DEFAULT 'manual'::character varying,
    verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    CONSTRAINT ck_kb_identity_connector_type CHECK (((connector_type)::text = ANY ((ARRAY['confluence'::character varying, 'notion'::character varying, 'google_drive'::character varying, 'slack'::character varying, 'github'::character varying, 'web_crawler'::character varying, 'jira'::character varying, 'sharepoint'::character varying])::text[]))),
    CONSTRAINT ck_kb_identity_mapping_source CHECK (((mapping_source)::text = ANY ((ARRAY['scim'::character varying, 'oidc_claim'::character varying, 'manual'::character varying, 'self_link'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS kb_connector_run_logs (
    id character varying(255),
    connector_id character varying(255),
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone,
    status character varying(32),
    trigger character varying(32) DEFAULT 'schedule'::character varying,
    docs_seen integer DEFAULT 0,
    docs_indexed integer DEFAULT 0,
    docs_failed integer DEFAULT 0,
    error_message text,
    CONSTRAINT ck_kb_connector_run_logs_status CHECK (((status)::text = ANY ((ARRAY['success'::character varying, 'failure'::character varying, 'running'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT ck_kb_connector_run_logs_trigger CHECK (((trigger)::text = ANY ((ARRAY['schedule'::character varying, 'manual'::character varying, 'initial'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS kb_connectors (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description character varying(1024),
    connector_type character varying(64),
    config jsonb DEFAULT '{}'::jsonb,
    collection_id character varying(255),
    schedule_cron character varying(128),
    enabled boolean DEFAULT true,
    auto_derive_from_oidc boolean DEFAULT false,
    next_run_at timestamp with time zone,
    last_run_started_at timestamp with time zone,
    last_run_finished_at timestamp with time zone,
    last_run_status character varying(32),
    last_run_error text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    CONSTRAINT ck_kb_connectors_status CHECK (((last_run_status IS NULL) OR ((last_run_status)::text = ANY ((ARRAY['success'::character varying, 'failure'::character varying, 'running'::character varying])::text[])))),
    CONSTRAINT ck_kb_connectors_type CHECK (((connector_type)::text = ANY ((ARRAY['confluence'::character varying, 'notion'::character varying, 'google_drive'::character varying, 'slack'::character varying, 'github'::character varying, 'web_crawler'::character varying, 'jira'::character varying, 'sharepoint'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id character varying(255),
    collection_id character varying(255),
    tenant_id character varying(255),
    name character varying(512),
    filename character varying(512),
    version character varying(50),
    author character varying(255),
    content_type character varying(100),
    file_size_bytes integer,
    raw_content bytea,
    original_content text,
    status character varying(20),
    error_message text,
    chunk_count integer DEFAULT 0,
    embedding_model character varying(255),
    source character varying(32) DEFAULT 'upload'::character varying,
    source_url character varying(2048),
    source_updated_at timestamp with time zone,
    connector_doc_key character varying(512),
    acl jsonb,
    metadata_json jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    CONSTRAINT ck_kb_documents_source CHECK (((source)::text = ANY ((ARRAY['upload'::character varying, 'connector'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS kubernetes_clusters (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    context_name character varying(255),
    api_server character varying(512),
    is_default boolean,
    created_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS kubernetes_deployments (
    id character varying(255),
    tenant_id character varying(255),
    deployment_name character varying(255),
    namespace character varying(255),
    mcp_server_id character varying(255),
    mcp_server_name character varying(255),
    image_id integer,
    image_name character varying(512),
    deployment_uid character varying(255),
    pod_selector character varying(255),
    service_name character varying(255),
    replicas integer,
    container_port integer,
    service_port integer,
    service_type character varying(50),
    cpu_request character varying(50),
    cpu_limit character varying(50),
    memory_request character varying(50),
    memory_limit character varying(50),
    github_repo character varying(512),
    deployed_by character varying(255),
    deployed_at timestamp with time zone,
    status character varying(50),
    last_status_check timestamp with time zone,
    health_status character varying(50),
    ready_replicas integer,
    available_replicas integer,
    restart_count integer,
    last_restart_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS llm_catalog (
    id character varying(255),
    provider_key character varying(64),
    model_name character varying(200),
    display_name character varying(255),
    description text,
    mode character varying(32),
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
    recommended boolean DEFAULT false,
    deprecated boolean DEFAULT false,
    source character varying(16) DEFAULT 'builtin'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS llm_model (
    id character varying(100),
    tenant_id character varying(255),
    name character varying(200),
    description text,
    usage text,
    provider_id character varying(64),
    model_name character varying(200),
    mode character varying(20),
    parameters double precision,
    tags json,
    api_url_auto_discovered boolean,
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
    thinking_enabled boolean DEFAULT false,
    routing_weight integer,
    rate_limit_rpm integer,
    rate_limit_tpm integer,
    routing_group_id character varying(100),
    cost_per_input_token double precision,
    cost_per_output_token double precision,
    cost_per_image double precision,
    supported_image_sizes jsonb,
    supported_image_qualities jsonb,
    max_images_per_request integer DEFAULT 1,
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
    source character varying(16) DEFAULT 'manual'::character varying,
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
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying
);

CREATE TABLE IF NOT EXISTS llm_provider_catalog (
    id character varying(64),
    key character varying(64),
    display_name character varying(255),
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
    source character varying(16) DEFAULT 'builtin'::character varying,
    supports_usage_import boolean DEFAULT false,
    import_api character varying(64),
    import_endpoint_tmpl character varying(512),
    import_capabilities json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS llm_providers (
    id character varying(64),
    tenant_id character varying(255),
    key character varying(64),
    display_name character varying(255),
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
    enabled boolean DEFAULT true,
    is_system boolean DEFAULT false,
    region character varying(32),
    catalog_id character varying(64),
    import_enabled boolean DEFAULT false,
    import_credentials_encrypted text,
    import_endpoint_override character varying(512),
    import_scope json,
    import_mapping_policy json,
    import_schedule_cron character varying(128),
    import_next_run_at timestamp with time zone,
    import_backfill_through date,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying
);

CREATE TABLE IF NOT EXISTS llm_routing_group (
    id character varying(100),
    tenant_id character varying(255),
    name character varying(200),
    description text,
    routing_strategy character varying(50),
    complexity_config json,
    complexity_floor_tier character varying(20),
    mode character varying(20) DEFAULT 'chat'::character varying,
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
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying
);

CREATE TABLE IF NOT EXISTS llm_routing_group_tier (
    id character varying(36),
    tenant_id character varying(255),
    routing_group_id character varying(36),
    tier character varying(20),
    llm_model_id character varying(36),
    "position" integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS llm_runner_machines (
    id character varying(100),
    tenant_id character varying(255),
    name character varying(200),
    description text,
    host character varying(255),
    port integer,
    adapter_type character varying(50),
    kube_context character varying(255),
    kube_namespace character varying(255),
    hf_token_secret character varying(255),
    default_prefill_accelerator_type character varying(50),
    default_decode_accelerator_type character varying(50),
    prefill_nodepool_labels json,
    decode_nodepool_labels json,
    kv_cache_pool_config json,
    default_tensor_parallel integer,
    status character varying(20),
    last_seen timestamp with time zone,
    last_error text,
    system_info json,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mcp_capability_snapshots (
    id character varying(64),
    tenant_id character varying(255),
    mcp_server_id character varying(255),
    snapshot_at timestamp with time zone DEFAULT now(),
    capabilities json,
    trigger character varying(32),
    content_hash character varying(64),
    is_change boolean DEFAULT false
);

CREATE TABLE IF NOT EXISTS mcp_catalog_entries (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(200),
    slug character varying(200),
    short_description text,
    description text,
    icon_url character varying(500),
    icon_data bytea,
    icon_mime_type character varying(100),
    icon_size integer,
    icon_hash character varying(64),
    repo_url character varying(500),
    runtime character varying(20),
    remote_config json,
    containerized_config json,
    env_vars json,
    tool_preview json,
    categories json,
    metadata_extra json,
    auth_pattern character varying(50),
    source character varying(20),
    enabled boolean,
    signature text,
    signature_algorithm character varying(50),
    signed_by character varying(50),
    signed_at timestamp with time zone,
    trust_tier character varying(20) DEFAULT 'unsigned'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mcp_registry_configs (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    url character varying(500),
    auth_type character varying(50),
    auth_token text,
    description text,
    enabled boolean,
    verified boolean,
    timeout integer,
    is_default boolean,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    last_sync timestamp with time zone
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(200),
    description text,
    base_url character varying(500),
    mcp_endpoint_path character varying(200),
    auth_type character varying(50),
    auth_token text,
    timeout double precision,
    max_concurrent_requests integer,
    retry_attempts integer,
    retry_after_ms integer,
    failure_threshold integer,
    recovery_timeout_secs integer,
    enabled boolean,
    mcp_apps_enabled boolean DEFAULT true,
    tenant_limits json,
    tenant_governance json,
    capabilities json,
    discovered_at timestamp with time zone,
    server_info json,
    protocol_version character varying(16),
    dialect character varying(16),
    wants_listener boolean DEFAULT true,
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
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying,
    CONSTRAINT ck_mcp_servers_max_concurrent_requests_positive CHECK (((max_concurrent_requests IS NULL) OR (max_concurrent_requests > 0)))
);

CREATE TABLE IF NOT EXISTS media_files (
    id character varying(64),
    tenant_id character varying(255),
    kind character varying(40),
    mime_type character varying(120),
    size_bytes bigint,
    content_hash character varying(64),
    object_key character varying(1024),
    storage_bucket_override character varying(255),
    chat_id character varying(64),
    model_id character varying(100),
    actor_id character varying(255),
    display_name character varying(500),
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone,
    deleted_at timestamp with time zone,
    access_count integer DEFAULT 0,
    last_accessed_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS notification_channels (
    id character varying(64),
    tenant_id character varying(255),
    name character varying(255),
    kind character varying(32),
    config_encrypted text,
    enabled boolean DEFAULT true,
    consecutive_failures integer DEFAULT 0,
    last_success_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    last_error text,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_nchan_kind CHECK (((kind)::text = ANY ((ARRAY['slack_webhook'::character varying, 'webhook'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id character varying(64),
    tenant_id character varying(255),
    subscription_id character varying(64),
    channel_id character varying(64),
    inbox_event_id character varying(64),
    inbox_item_id character varying(64),
    fingerprint character varying(512),
    status character varying(16),
    attempts integer DEFAULT 0,
    next_attempt_at timestamp with time zone,
    last_error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_ndel_status CHECK (((status)::text = ANY ((ARRAY['sent'::character varying, 'failed'::character varying, 'coalesced'::character varying, 'skipped'::character varying, 'pending'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS notification_subscriptions (
    id character varying(64),
    tenant_id character varying(255),
    channel_id character varying(64),
    event_class character varying(32),
    severity_floor character varying(16) DEFAULT 'warning'::character varying,
    categories jsonb,
    ai_system_id character varying(255),
    coalesce_seconds integer DEFAULT 900,
    enabled boolean DEFAULT true,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_nsub_event_class CHECK (((event_class)::text = ANY ((ARRAY['inbox_item_created'::character varying, 'inbox_item_reoccurred'::character varying, 'digest_weekly'::character varying])::text[]))),
    CONSTRAINT ck_nsub_severity_floor CHECK (((severity_floor)::text = ANY ((ARRAY['info'::character varying, 'warning'::character varying, 'critical'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS notifications (
    id character varying(64),
    tenant_id character varying(255),
    user_id integer,
    notification_type character varying(50),
    title character varying(500),
    body text,
    metadata json,
    is_read boolean DEFAULT false,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS oauth_app_clients (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    app_type character varying(50),
    idp_id character varying(255),
    client_id character varying(500),
    encrypted_client_secret text,
    redirect_uri text,
    scopes json,
    client_type character varying(50),
    supports_pkce boolean,
    allowed_grant_types json,
    description text,
    registered_at timestamp with time zone DEFAULT now(),
    registered_by character varying(50),
    last_used timestamp with time zone,
    is_active boolean DEFAULT true,
    auto_registered boolean DEFAULT false,
    registration_client_uri character varying(500),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_cimd_clients (
    id character varying(255),
    tenant_id character varying(255),
    idp_id character varying(255),
    cimd_url character varying(2048),
    idp_client_id character varying(512),
    idp_client_secret_encrypted text,
    doc json,
    doc_fetched_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    gateway_user_id character varying(255),
    idp_user_id character varying(255),
    mcp_server_id character varying(255),
    idp_id character varying(255),
    client_id character varying(500),
    encrypted_client_secret text,
    encrypted_access_token text,
    encrypted_refresh_token text,
    token_type character varying(50),
    expires_at timestamp without time zone,
    scopes text,
    issuer character varying(500),
    revoked boolean,
    revoked_at timestamp with time zone,
    created_at timestamp without time zone DEFAULT now(),
    last_refreshed_at timestamp without time zone,
    refresh_count integer,
    last_actor_agent_id character varying(255),
    last_used_by_agent_at timestamp with time zone,
    updated_at timestamp without time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS observed_agent_events (
    id character varying(255),
    tenant_id character varying(255),
    event_id character varying(255),
    collector_id character varying(255),
    device_id character varying(128),
    device_name character varying(255),
    client character varying(64),
    session_id character varying(255),
    tool_name character varying(255),
    tool_kind character varying(16) DEFAULT 'builtin'::character varying,
    mcp_server character varying(255),
    phase character varying(8),
    decision character varying(16),
    occurred_at timestamp with time zone,
    duration_ms integer,
    exit_status integer,
    project character varying(255),
    source character varying(32) DEFAULT 'endpoint'::character varying,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform_measurement_changes (
    id character varying(64),
    metric_keys jsonb,
    bases jsonb,
    reason text,
    introduced_in_version character varying(32),
    platform_version character varying(64),
    applied_at timestamp with time zone DEFAULT now(),
    epochs_written integer DEFAULT 0
);

CREATE TABLE IF NOT EXISTS platform_settings (
    id character varying(32),
    global_key_allowed_target_tenants json,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS policy_bundle_events (
    id character varying(255),
    tenant_id character varying(255),
    bundle_id character varying(255),
    scope_group_id character varying(255),
    event_type policyeventtype,
    diff json,
    dry_run_result json,
    admin_id character varying(255),
    admin_type character varying(20),
    ip_address character varying(50),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_bundles (
    id character varying(255),
    tenant_id character varying(255),
    scope_group_id character varying(255),
    scope_group_path character varying(1000),
    name character varying(255),
    description text,
    version character varying(50),
    labels json,
    yaml_content text,
    resolved_config json,
    status policybundlestatus,
    is_active boolean,
    diff_summary json,
    source character varying(50),
    source_ref character varying(500),
    applied_at timestamp with time zone,
    applied_by character varying(255),
    rolled_back_at timestamp with time zone,
    rolled_back_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS policy_templates (
    id character varying(255),
    name character varying(255),
    description text,
    category character varying(100),
    tags json,
    yaml_content text,
    is_system boolean,
    tenant_id character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_cache_policies (
    id character varying(50),
    tenant_id character varying(255),
    name character varying(255),
    description text,
    enabled boolean DEFAULT true,
    strategy character varying(40) DEFAULT 'tools_and_system'::character varying,
    ttl_seconds integer DEFAULT 300,
    min_request_tokens integer DEFAULT 1024,
    sticky_routing boolean DEFAULT true,
    volatile_patterns json DEFAULT '[]'::json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS proxy_logs (
    id character varying(255),
    tenant_id character varying(255),
    "timestamp" timestamp with time zone DEFAULT now(),
    proxy_type character varying(20),
    proxy_subtype character varying(50),
    skill_id character varying(255),
    skill_execution_id character varying(255),
    correlation_id character varying(255),
    parent_request_id character varying(255),
    request_id character varying(255),
    trace_root_kind character varying(32),
    trace_continuity character varying(32),
    source character varying(16) DEFAULT 'gateway'::character varying,
    record_id character varying(64),
    run_end_state character varying(24),
    outcome_id character varying(128),
    region_routed_to character varying(32),
    residency_class character varying(32),
    compliance_tags jsonb,
    guardrail_blocked boolean DEFAULT false,
    argument_policy_denied boolean DEFAULT false,
    pii_detected boolean DEFAULT false,
    is_auth_event boolean DEFAULT false,
    gateway_user_id character varying(255),
    gateway_user_username character varying(255),
    idp_user_id character varying(255),
    idp_user_username character varying(255),
    idp_user_email character varying(255),
    end_user_session_id character varying(255),
    resource_group_id character varying(255),
    api_key_id character varying(255),
    api_key_name character varying(255),
    client_ip character varying(45),
    user_agent text,
    server_id character varying(255),
    idp_server_id character varying(255),
    http_method character varying(10),
    endpoint text,
    target_url text,
    request_size_bytes integer,
    request_headers text,
    request_body_preview text,
    http_status_code integer,
    response_size_bytes integer,
    response_headers text,
    response_body_preview text,
    success boolean,
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
    created_at timestamp with time zone DEFAULT now(),
    credential_kind character varying(16),
    credential_assurance character varying(16),
    credential_ref character varying(512),
    credential_policy_outcome character varying(16),
    CONSTRAINT ck_proxy_logs_agent_impl_source CHECK (((agent_impl_source)::text = ANY ((ARRAY['token'::character varying, 'mcp_client_info'::character varying, 'baggage'::character varying, 'header'::character varying])::text[]))),
    CONSTRAINT ck_proxy_logs_chain_version CHECK (((chain_version)::text = 'v5'::text)),
    CONSTRAINT ck_proxy_logs_credential_assurance CHECK (((credential_assurance IS NULL) OR ((credential_assurance)::text = ANY ((ARRAY['key'::character varying, 'bound_key'::character varying, 'workload'::character varying, 'workload_pop'::character varying])::text[])))),
    CONSTRAINT ck_proxy_logs_credential_kind CHECK (((credential_kind IS NULL) OR ((credential_kind)::text = ANY ((ARRAY['api_key'::character varying, 'workload_token'::character varying, 'x509_svid'::character varying, 'brutor_token'::character varying, 'human_session'::character varying, 'platform_service'::character varying, 'anonymous'::character varying])::text[])))),
    CONSTRAINT ck_proxy_logs_credential_outcome CHECK (((credential_policy_outcome IS NULL) OR ((credential_policy_outcome)::text = ANY ((ARRAY['met'::character varying, 'below_policy'::character varying, 'refused'::character varying, 'exempt'::character varying])::text[]))))
);

CREATE TABLE IF NOT EXISTS proxy_metrics (
    id character varying(255),
    tenant_id character varying(255),
    "timestamp" timestamp with time zone,
    proxy_type character varying(20),
    source character varying(40) DEFAULT 'gateway'::character varying,
    request_id character varying(255),
    correlation_id character varying(255),
    end_user_type character varying(20),
    end_user_username character varying(255),
    gateway_user_id character varying(255),
    gateway_username character varying(255),
    client_ip character varying(45),
    user_agent_hash character varying(64),
    server_id character varying(255),
    virtual_server_id character varying(255),
    server_url text,
    http_method character varying(10),
    endpoint text,
    endpoint_category character varying(50),
    total_duration_ms double precision,
    connect_time_ms double precision,
    handshake_time_ms double precision,
    processing_time_ms double precision,
    response_time_ms double precision,
    request_size_bytes integer,
    response_size_bytes integer,
    request_headers_count integer,
    response_headers_count integer,
    http_status_code integer,
    success boolean,
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
    request_hour integer,
    request_day_of_week integer,
    timezone_offset integer
);

CREATE TABLE IF NOT EXISTS resource_group_agent_cards (
    id character varying(255),
    resource_group_id character varying(255),
    agent_card_id character varying(255),
    limits json,
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_agent_members (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    agent_id character varying(255),
    group_id character varying(255),
    role character varying(50),
    added_by character varying(255),
    joined_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resource_group_agent_skills (
    id character varying(255),
    resource_group_id character varying(255),
    agent_skill_id character varying(255),
    created_at timestamp with time zone,
    created_by character varying(255),
    limits json,
    governance json
);

CREATE TABLE IF NOT EXISTS resource_group_argument_policies (
    resource_group_id character varying(255),
    argument_policy_id character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resource_group_cache_configs (
    resource_group_id character varying(255),
    cache_config_id character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_deployment_machines (
    id character varying(255),
    resource_group_id character varying(255),
    machine_id character varying(100),
    created_at timestamp with time zone,
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_end_user_groups (
    resource_group_id character varying(255),
    end_user_group_id character varying(64),
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_guardrails (
    resource_group_id character varying(255),
    guardrail_config_id character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_kb_collections (
    resource_group_id character varying(255),
    kb_collection_id character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_kb_connectors (
    resource_group_id character varying(255),
    kb_connector_id character varying(255),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resource_group_llm_governance (
    id character varying(255),
    tenant_id character varying(255),
    resource_group_id character varying(255),
    llm_model_id character varying(255),
    governance json,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_llm_models (
    id character varying(255),
    resource_group_id character varying(255),
    llm_model_id character varying(255),
    created_at timestamp with time zone,
    created_by character varying(255),
    limits json,
    portal_visible boolean DEFAULT true,
    auto_bound boolean DEFAULT false
);

CREATE TABLE IF NOT EXISTS resource_group_mcp_capabilities (
    id character varying(255),
    tenant_id character varying(255),
    resource_group_id character varying(255),
    server_config_id character varying(255),
    capability_type character varying(50),
    capability_name character varying(500),
    access character varying(20),
    approval_timeout_seconds integer,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_memberships (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    user_id integer,
    group_id character varying(255),
    role character varying(50),
    added_by character varying(50),
    joined_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resource_group_prompt_cache_policies (
    resource_group_id character varying(50),
    prompt_cache_policy_id character varying(50),
    model_id character varying(50)
);

CREATE TABLE IF NOT EXISTS resource_group_semantic_policies (
    resource_group_id character varying(255),
    semantic_policy_id character varying(255)
);

CREATE TABLE IF NOT EXISTS resource_group_server_configs (
    id character varying(255),
    resource_group_id character varying(255),
    server_config_id character varying(255),
    created_at timestamp with time zone,
    created_by character varying(255),
    limits json
);

CREATE TABLE IF NOT EXISTS resource_groups (
    id character varying(255),
    tenant_id character varying(255),
    parent_group_id character varying(255),
    hierarchy_level integer,
    hierarchy_path character varying(1000),
    inherit_from_parent boolean,
    name character varying(255),
    display_name character varying(255),
    description text,
    icon character varying(100),
    color character varying(50),
    group_type grouptype,
    system_kind character varying(24),
    baseline_opt_out boolean DEFAULT false,
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
    enforce_accepted_callers boolean DEFAULT false,
    lifecycle_stage character varying(32),
    rollup_exclusion jsonb,
    successor_system_id character varying(255),
    no_successor_declared boolean,
    is_active boolean,
    is_system boolean,
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
    phi_resource boolean DEFAULT false,
    sensitive_data boolean DEFAULT false,
    source character varying(16) DEFAULT 'manual'::character varying,
    source_external_id character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255),
    version character varying(50) DEFAULT '1.0'::character varying,
    identity_policy jsonb,
    CONSTRAINT ck_resource_groups_approval_window CHECK (((approval_window_seconds IS NULL) OR ((approval_window_seconds >= 60) AND (approval_window_seconds <= 604800))))
);

CREATE TABLE IF NOT EXISTS router_cache_configs (
    tenant_id character varying(64),
    enabled boolean DEFAULT true,
    ttl_secs integer DEFAULT 300,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT router_cache_configs_ttl_range CHECK (((ttl_secs >= 0) AND (ttl_secs <= 86400)))
);

CREATE TABLE IF NOT EXISTS run_interventions (
    id character varying(255),
    tenant_id character varying(255),
    root_task_id character varying(255),
    action character varying(16) DEFAULT 'abort'::character varying,
    reason text,
    requested_at timestamp with time zone DEFAULT now(),
    first_blocked_at timestamp with time zone,
    steps_blocked integer DEFAULT 0,
    status character varying(16) DEFAULT 'requested'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS saved_log_views (
    id character varying(64),
    tenant_id character varying(255),
    scope character varying(32),
    owner_admin_id character varying(255),
    name character varying(255),
    description text,
    filters jsonb,
    is_shared boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_saved_log_views_scope CHECK (((scope)::text = ANY ((ARRAY['proxy_logs'::character varying, 'admin_audit_logs'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS scheduler_runtime_state (
    scheduler_name character varying(64),
    paused boolean DEFAULT false,
    paused_at timestamp with time zone,
    paused_by character varying(255),
    pause_reason text,
    resumed_at timestamp with time zone,
    resumed_by character varying(255),
    last_beacon_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_version (
    id integer,
    schema_version character varying(50),
    app_version character varying(50),
    schema_created_at timestamp with time zone DEFAULT now(),
    app_started_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scim_tokens (
    id character varying(64),
    tenant_id character varying(255),
    name character varying(255),
    token_hash character varying(128),
    token_prefix character varying(16),
    idp_server_id character varying(255),
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    created_by character varying(64)
);

CREATE TABLE IF NOT EXISTS search_provider_configs (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    provider character varying(50),
    enabled boolean,
    api_key_encrypted text,
    google_cx_id character varying(255),
    searxng_base_url character varying(500),
    max_results_per_query integer,
    safe_search boolean,
    daily_query_limit integer,
    allowed_domains json,
    blocked_domains json,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semantic_cache_configs (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description character varying(1024),
    enabled boolean,
    exact_match_enabled boolean,
    cache_key_mode character varying(20) DEFAULT 'last_user_turn'::character varying,
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
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS semantic_cache_tool_rules (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    cache_config_id character varying(255),
    tenant_id character varying(255),
    server_config_id character varying(255),
    capability_type character varying(20),
    capability_name character varying(500),
    cacheable boolean,
    priority integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semantic_policies (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description text,
    constraint_text text,
    action character varying(16) DEFAULT 'block'::character varying,
    mode character varying(16) DEFAULT 'shadow'::character varying,
    surfaces jsonb,
    judge_model_id character varying(255),
    fail_open boolean DEFAULT false,
    enabled boolean DEFAULT true,
    created_by character varying(255),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS server_configs (
    id character varying(255),
    tenant_id character varying(255),
    mcp_server_id character varying(255),
    virtual_server_id character varying(255),
    oauth_client_id character varying(255),
    name character varying(255),
    description text,
    priority integer,
    is_default boolean,
    enabled boolean,
    display_name character varying(255),
    display_description text,
    icon_url character varying(512),
    usage_count integer,
    last_used timestamp without time zone,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255)
);

CREATE TABLE IF NOT EXISTS session_budget_resets (
    tenant_id character varying(255),
    session_id character varying(255),
    reset_at timestamp with time zone DEFAULT now(),
    reset_by character varying(255),
    reason text
);

CREATE TABLE IF NOT EXISTS skill_sources (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    repo_url character varying(1024),
    git_ref character varying(255) DEFAULT 'main'::character varying,
    subpath character varying(512),
    enabled boolean DEFAULT true,
    sync_interval_seconds integer DEFAULT 3600,
    auto_publish boolean DEFAULT false,
    status character varying(50) DEFAULT 'idle'::character varying,
    last_synced_at timestamp with time zone,
    last_attempted_at timestamp with time zone,
    last_commit_sha character varying(64),
    sync_error text,
    discovered_skill_count integer DEFAULT 0,
    github_token_secret_ref character varying(512),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255)
);

CREATE TABLE IF NOT EXISTS system_admin_audit_logs (
    id character varying(50),
    admin_id character varying(50),
    action character varying(100),
    resource_type character varying(50),
    resource_id character varying(100),
    affected_tenant_id character varying(255),
    ip_address character varying(45),
    user_agent text,
    request_method character varying(10),
    request_path character varying(500),
    success boolean,
    details json,
    "timestamp" timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_admin_sessions (
    id character varying(50),
    admin_id character varying(50),
    token_hash character varying(255),
    ip_address character varying(45),
    user_agent text,
    risk_score double precision,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone,
    last_activity timestamp with time zone DEFAULT now(),
    is_active boolean,
    deactivated_at timestamp with time zone,
    deactivation_reason character varying(100)
);

CREATE TABLE IF NOT EXISTS system_admins (
    id character varying(50),
    username character varying(100),
    email character varying(255),
    password_hash character varying(255),
    full_name character varying(200),
    is_active boolean,
    is_locked boolean,
    failed_login_attempts integer,
    locked_until timestamp with time zone,
    permissions json,
    last_login timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(50),
    totp_secret text,
    totp_enabled boolean DEFAULT false,
    totp_verified_at timestamp with time zone,
    backup_codes json
);

CREATE TABLE IF NOT EXISTS system_alerts (
    id character varying(64),
    tenant_id character varying(255),
    severity character varying(16),
    category character varying(32),
    source character varying(255),
    resource_type character varying(32),
    resource_id character varying(255),
    resource_name character varying(255),
    error_code character varying(64),
    title character varying(255),
    message text,
    context jsonb,
    status character varying(20) DEFAULT 'active'::character varying,
    first_seen_at timestamp with time zone DEFAULT now(),
    last_seen_at timestamp with time zone DEFAULT now(),
    occurrence_count integer DEFAULT 1,
    acknowledged_at timestamp with time zone,
    acknowledged_by character varying(255),
    resolved_at timestamp with time zone,
    resolved_by character varying(255),
    resolution_note text,
    dedup_key character varying(512),
    CONSTRAINT ck_system_alerts_category CHECK (((category)::text = ANY ((ARRAY['downstream_provider'::character varying, 'downstream_idp'::character varying, 'downstream_cache'::character varying, 'downstream_vector_db'::character varying, 'resource_health'::character varying, 'infrastructure_threshold'::character varying, 'internal_error'::character varying, 'ai_system_liveness'::character varying, 'ai_system_behaviour'::character varying, 'ai_system_cost'::character varying, 'ai_system_dependency'::character varying])::text[]))),
    CONSTRAINT ck_system_alerts_severity CHECK (((severity)::text = ANY ((ARRAY['critical'::character varying, 'high'::character varying, 'medium'::character varying, 'low'::character varying])::text[]))),
    CONSTRAINT ck_system_alerts_status CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'acknowledged'::character varying, 'resolved'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS system_operation_log (
    id character varying(64),
    tenant_id character varying(255),
    actor_admin_id character varying(255),
    actor_type character varying(32),
    action character varying(64),
    scope_kind character varying(32),
    scope_id character varying(255),
    payload jsonb,
    before_state jsonb,
    after_state jsonb,
    ip inet,
    user_agent text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ck_system_operation_log_actor_type CHECK (((actor_type)::text = ANY ((ARRAY['system_admin'::character varying, 'tenant_admin'::character varying, 'system'::character varying])::text[])))
);

CREATE TABLE IF NOT EXISTS tenant_a2a_keys (
    tenant_id character varying(255),
    public_key text,
    private_key_encrypted text,
    key_algorithm character varying(32) DEFAULT 'ed25519'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    rotated_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS tenant_admin_audit_logs (
    id character varying(50),
    tenant_id character varying(255),
    admin_id character varying(50),
    action character varying(100),
    resource_type character varying(50),
    resource_id character varying(100),
    ip_address character varying(45),
    user_agent text,
    request_method character varying(10),
    request_path character varying(500),
    success boolean,
    details json,
    "timestamp" timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_admin_sessions (
    id character varying(50),
    tenant_id character varying(255),
    admin_id character varying(50),
    token_hash character varying(255),
    ip_address character varying(45),
    user_agent text,
    risk_score double precision,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone,
    last_activity timestamp with time zone DEFAULT now(),
    is_active boolean,
    deactivated_at timestamp with time zone,
    deactivation_reason character varying(100)
);

CREATE TABLE IF NOT EXISTS tenant_admins (
    id character varying(50),
    tenant_id character varying(255),
    role_id character varying(50),
    username character varying(100),
    email character varying(255),
    password_hash character varying(255),
    full_name character varying(200),
    is_active boolean,
    is_locked boolean,
    failed_login_attempts integer,
    locked_until timestamp with time zone,
    last_login timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(50),
    avatar_url character varying(500),
    photo_data bytea,
    photo_mime_type character varying(100),
    photo_size integer,
    photo_hash character varying(64),
    totp_secret text,
    totp_enabled boolean,
    totp_verified_at timestamp with time zone,
    backup_codes json,
    password_change_required boolean DEFAULT false,
    idp_server_id character varying(255),
    idp_subject character varying(512),
    idp_last_login timestamp with time zone
);

CREATE TABLE IF NOT EXISTS tenant_role_permissions (
    id character varying(50),
    role_id character varying(50),
    permission character varying(100),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_roles (
    id character varying(50),
    tenant_id character varying(255),
    name character varying(100),
    description text,
    is_system boolean,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(50)
);

CREATE TABLE IF NOT EXISTS tenants (
    id character varying(255),
    name character varying(255),
    display_name character varying(255),
    organization_name character varying(500),
    domain character varying(255),
    primary_contact_email character varying(255),
    primary_contact_name character varying(255),
    is_active boolean,
    is_default boolean,
    is_trial boolean,
    trial_ends_at timestamp with time zone,
    suspended boolean,
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
    features json,
    settings json,
    ai_system_gate_policy json,
    assurance_capture_interval_hours integer,
    default_approval_window_seconds integer,
    mfa_policy character varying(20) DEFAULT 'optional'::character varying,
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
    kb_oidc_auto_derive_enabled boolean DEFAULT true,
    maintenance_mode character varying(16) DEFAULT 'off'::character varying,
    maintenance_mode_at timestamp with time zone,
    maintenance_mode_by character varying(255),
    drain_grace_secs integer,
    data_residency jsonb,
    data_retention jsonb,
    compliance_profile jsonb DEFAULT '{"frameworks": []}'::jsonb,
    created_by character varying(50),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    last_active_at timestamp with time zone,
    identity_policy_default jsonb,
    CONSTRAINT ck_tenants_default_approval_window CHECK (((default_approval_window_seconds IS NULL) OR ((default_approval_window_seconds >= 60) AND (default_approval_window_seconds <= 604800))))
);

CREATE TABLE IF NOT EXISTS tool_approval_requests (
    id character varying(64),
    tenant_id character varying(255),
    server_config_id character varying(255),
    server_name character varying(500),
    capability_type character varying(50),
    capability_name character varying(500),
    requester_type character varying(20),
    requester_user_id character varying(255),
    requester_api_key_id character varying(255),
    requester_group_id character varying(255),
    requester_display character varying(500),
    call_arguments json,
    request_metadata json,
    status character varying(20),
    approval_token character varying(128),
    token_used character varying(1) DEFAULT 'N'::character varying,
    timeout_seconds integer,
    expires_at timestamp with time zone,
    resolved_by character varying(255),
    resolved_at timestamp with time zone,
    resolution_note text,
    originating_trace_root_id character varying(64),
    originating_parent_task_id character varying(64),
    originating_trace_root_kind character varying(64),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_alerts (
    id character varying(64),
    tenant_id character varying(255),
    resource_type character varying(20),
    group_id character varying(255),
    group_name character varying(255),
    alert_type character varying(20),
    metric character varying(50),
    threshold_pct integer,
    current_value bigint,
    current_value_float double precision,
    limit_value bigint,
    limit_value_float double precision,
    utilization_pct double precision,
    period character varying(20),
    period_start timestamp with time zone,
    status character varying(20),
    notified_at timestamp with time zone,
    acknowledged_by character varying(255),
    acknowledged_at timestamp with time zone,
    resolved_at timestamp with time zone,
    resolution_note text,
    triggered_by_request_id character varying(255),
    message text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_import_checkpoints (
    provider_id character varying(64),
    tenant_id character varying(255),
    last_imported_through timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_import_run_logs (
    id character varying(64),
    tenant_id character varying(255),
    provider_id character varying(64),
    import_api character varying(64),
    trigger character varying(16),
    status character varying(16),
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    rows_ingested integer DEFAULT 0,
    users_created integer DEFAULT 0,
    groups_created integer DEFAULT 0,
    models_created integer DEFAULT 0,
    memberships_created integer DEFAULT 0,
    model_links_created integer DEFAULT 0,
    cost_imported double precision DEFAULT '0'::double precision,
    currency character varying(10) DEFAULT 'USD'::character varying,
    error_message text,
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id integer GENERATED BY DEFAULT AS IDENTITY,
    tenant_id character varying(255),
    user_id integer,
    group_id character varying(255),
    ancestry_path character varying(1000),
    group_name character varying(255),
    resource_type character varying(50),
    resource_id character varying(255),
    resource_name character varying(255),
    is_inherited boolean,
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
    request_id character varying(255),
    status_code integer,
    error_message text,
    response_time_ms integer,
    http_method character varying(10),
    endpoint character varying(255),
    client_ip character varying(45),
    user_agent character varying(255),
    source character varying(40) DEFAULT 'gateway'::character varying,
    ingest_grain character varying(16),
    import_run_id character varying(64),
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS video_generation_jobs (
    id character varying(64),
    tenant_id character varying(255),
    model_id character varying(100),
    actor_id character varying(255),
    prompt text,
    resolution character varying(40),
    duration_seconds integer,
    chat_id character varying(64),
    status character varying(20) DEFAULT 'pending'::character varying,
    provider_request_id character varying(255),
    output_file_id character varying(64),
    error text,
    cost_usd double precision,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS virtual_servers (
    id character varying(255),
    tenant_id character varying(255),
    name character varying(255),
    description text,
    capabilities json,
    owner character varying(50),
    enabled boolean,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS witnesses (
    id character varying(64),
    scope character varying(16),
    tenant_id character varying(255),
    kind character varying(16),
    url text,
    operator_label character varying(255),
    pinned_key text,
    enabled boolean DEFAULT true,
    same_operator boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    created_by character varying(255),
    updated_by character varying(255)
);


-- Primary keys, unique constraints and unique indexes (including
-- partial ones). ON CONFLICT infers its arbiter from these.

CREATE UNIQUE INDEX IF NOT EXISTS a2a_push_notification_configs_pk_ix ON a2a_push_notification_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS a2a_tasks_pk_ix ON a2a_tasks (id);

CREATE UNIQUE INDEX IF NOT EXISTS action_records_pk_ix ON action_records (record_id);

CREATE UNIQUE INDEX IF NOT EXISTS admin_stream_tickets_pk_ix ON admin_stream_tickets (ticket_hash);

CREATE UNIQUE INDEX IF NOT EXISTS agent_card_capabilities_pk_ix ON agent_card_capabilities (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_cards_pk_ix ON agent_cards (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_grants_pk_ix ON agent_grants (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_identities_pk_ix ON agent_identities (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_identity_approval_history_pk_ix ON agent_identity_approval_history (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_implementation_releases_pk_ix ON agent_implementation_releases (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_skill_assets_pk_ix ON agent_skill_assets (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_skill_dependencies_pk_ix ON agent_skill_dependencies (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_skill_executions_pk_ix ON agent_skill_executions (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_skill_resources_pk_ix ON agent_skill_resources (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_skill_scripts_pk_ix ON agent_skill_scripts (id);

CREATE UNIQUE INDEX IF NOT EXISTS agent_skills_pk_ix ON agent_skills (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_asset_factsheets_pk_ix ON ai_asset_factsheets (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_assets_pk_ix ON ai_assets (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_assurance_reports_pk_ix ON ai_system_assurance_reports (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_baseline_epochs_pk_ix ON ai_system_baseline_epochs (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_baselines_pk_ix ON ai_system_baselines (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_compliance_profiles_pk_ix ON ai_system_compliance_profiles (ai_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_contracts_pk_ix ON ai_system_contracts (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_data_scopes_pk_ix ON ai_system_data_scopes (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_dependencies_pk_ix ON ai_system_dependencies (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_drift_events_pk_ix ON ai_system_drift_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_evidence_pk_ix ON ai_system_evidence (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_health_pk_ix ON ai_system_health (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_incident_window_findings_pk_ix ON ai_system_incident_window_findings (incident_window_id, drift_event_id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_incident_windows_pk_ix ON ai_system_incident_windows (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_lifecycle_events_pk_ix ON ai_system_lifecycle_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_liveness_expectations_pk_ix ON ai_system_liveness_expectations (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_reach_rules_pk_ix ON ai_system_reach_rules (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_record_access_pk_ix ON ai_system_record_access (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_replay_cases_pk_ix ON ai_system_replay_cases (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_replay_runs_pk_ix ON ai_system_replay_runs (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_replay_suites_pk_ix ON ai_system_replay_suites (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_response_policies_pk_ix ON ai_system_response_policies (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_run_feedback_pk_ix ON ai_system_run_feedback (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_run_scopes_pk_ix ON ai_system_run_scopes (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_run_segments_pk_ix ON ai_system_run_segments (id);

CREATE UNIQUE INDEX IF NOT EXISTS ai_system_runs_pk_ix ON ai_system_runs (id);

CREATE UNIQUE INDEX IF NOT EXISTS api_keys_pk_ix ON api_keys (id);

CREATE UNIQUE INDEX IF NOT EXISTS argument_policies_pk_ix ON argument_policies (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_check_results_pk_ix ON assurance_check_results (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_check_reviews_pk_ix ON assurance_check_reviews (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_check_rubrics_pk_ix ON assurance_check_rubrics (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_check_versions_pk_ix ON assurance_check_versions (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_checks_pk_ix ON assurance_checks (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_digests_pk_ix ON assurance_digests (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_inbox_events_pk_ix ON assurance_inbox_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS assurance_inbox_items_pk_ix ON assurance_inbox_items (id);

CREATE UNIQUE INDEX IF NOT EXISTS audit_chain_checkpoints_pk_ix ON audit_chain_checkpoints (id);

CREATE UNIQUE INDEX IF NOT EXISTS audit_chain_prunes_pk_ix ON audit_chain_prunes (id);

CREATE UNIQUE INDEX IF NOT EXISTS audit_export_destinations_pk_ix ON audit_export_destinations (id);

CREATE UNIQUE INDEX IF NOT EXISTS batch_jobs_pk_ix ON batch_jobs (id);

CREATE UNIQUE INDEX IF NOT EXISTS batch_requests_pk_ix ON batch_requests (id);

CREATE UNIQUE INDEX IF NOT EXISTS collector_keys_pk_ix ON collector_keys (key_id);

CREATE UNIQUE INDEX IF NOT EXISTS compliance_documentation_exports_pk_ix ON compliance_documentation_exports (id);

CREATE UNIQUE INDEX IF NOT EXISTS compliance_frameworks_pk_ix ON compliance_frameworks (id);

CREATE UNIQUE INDEX IF NOT EXISTS compliance_incident_events_pk_ix ON compliance_incident_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS compliance_incidents_pk_ix ON compliance_incidents (id);

CREATE UNIQUE INDEX IF NOT EXISTS compliance_notice_configs_pk_ix ON compliance_notice_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS compliance_reports_pk_ix ON compliance_reports (id);

CREATE UNIQUE INDEX IF NOT EXISTS data_erasure_events_pk_ix ON data_erasure_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS disclosure_records_pk_ix ON disclosure_records (id);

CREATE UNIQUE INDEX IF NOT EXISTS discovered_assets_pk_ix ON discovered_assets (id);

CREATE UNIQUE INDEX IF NOT EXISTS discovery_events_pk_ix ON discovery_events (event_id, tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS discovery_evidence_pk_ix ON discovery_evidence (id);

CREATE UNIQUE INDEX IF NOT EXISTS docker_deployments_pk_ix ON docker_deployments (id);

CREATE UNIQUE INDEX IF NOT EXISTS docker_images_pk_ix ON docker_images (id);

CREATE UNIQUE INDEX IF NOT EXISTS ecs_deployments_pk_ix ON ecs_deployments (id);

CREATE UNIQUE INDEX IF NOT EXISTS edition_marker_pk_ix ON edition_marker (id);

CREATE UNIQUE INDEX IF NOT EXISTS end_user_group_members_pk_ix ON end_user_group_members (end_user_group_id, end_user_id);

CREATE UNIQUE INDEX IF NOT EXISTS end_user_groups_pk_ix ON end_user_groups (id);

CREATE UNIQUE INDEX IF NOT EXISTS end_user_revoked_tokens_pk_ix ON end_user_revoked_tokens (jti);

CREATE UNIQUE INDEX IF NOT EXISTS end_users_pk_ix ON end_users (id);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_bundles_pk_ix ON evidence_bundles (id);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_logs_pk_ix ON evidence_logs (log_id);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_receipts_pk_ix ON evidence_receipts (id);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_settings_pk_ix ON evidence_settings (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_signing_keys_pk_ix ON evidence_signing_keys (kid);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_tree_heads_pk_ix ON evidence_tree_heads (id);

CREATE UNIQUE INDEX IF NOT EXISTS guardrail_break_glass_pk_ix ON guardrail_break_glass (id);

CREATE UNIQUE INDEX IF NOT EXISTS guardrail_configs_pk_ix ON guardrail_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS health_check_configs_pk_ix ON health_check_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS idp_servers_pk_ix ON idp_servers (id);

CREATE UNIQUE INDEX IF NOT EXISTS iso42001_control_coverage_pk_ix ON iso42001_control_coverage (id);

CREATE UNIQUE INDEX IF NOT EXISTS iso42001_controls_pk_ix ON iso42001_controls (control_id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_chunks_pk_ix ON kb_chunks (id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_collections_pk_ix ON kb_collections (id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_connector_checkpoints_pk_ix ON kb_connector_checkpoints (connector_id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_connector_credentials_pk_ix ON kb_connector_credentials (id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_connector_identity_mappings_pk_ix ON kb_connector_identity_mappings (id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_connector_run_logs_pk_ix ON kb_connector_run_logs (id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_connectors_pk_ix ON kb_connectors (id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_documents_pk_ix ON kb_documents (id);

CREATE UNIQUE INDEX IF NOT EXISTS kubernetes_clusters_pk_ix ON kubernetes_clusters (id);

CREATE UNIQUE INDEX IF NOT EXISTS kubernetes_deployments_pk_ix ON kubernetes_deployments (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_catalog_pk_ix ON llm_catalog (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_model_pk_ix ON llm_model (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_provider_catalog_pk_ix ON llm_provider_catalog (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_providers_pk_ix ON llm_providers (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_routing_group_pk_ix ON llm_routing_group (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_routing_group_tier_pk_ix ON llm_routing_group_tier (id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_runner_machines_pk_ix ON llm_runner_machines (id);

CREATE UNIQUE INDEX IF NOT EXISTS mcp_capability_snapshots_pk_ix ON mcp_capability_snapshots (id);

CREATE UNIQUE INDEX IF NOT EXISTS mcp_catalog_entries_pk_ix ON mcp_catalog_entries (id);

CREATE UNIQUE INDEX IF NOT EXISTS mcp_registry_configs_pk_ix ON mcp_registry_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS mcp_servers_pk_ix ON mcp_servers (id);

CREATE UNIQUE INDEX IF NOT EXISTS media_files_pk_ix ON media_files (id);

CREATE UNIQUE INDEX IF NOT EXISTS notification_channels_pk_ix ON notification_channels (id);

CREATE UNIQUE INDEX IF NOT EXISTS notification_deliveries_pk_ix ON notification_deliveries (id);

CREATE UNIQUE INDEX IF NOT EXISTS notification_subscriptions_pk_ix ON notification_subscriptions (id);

CREATE UNIQUE INDEX IF NOT EXISTS notifications_pk_ix ON notifications (id);

CREATE UNIQUE INDEX IF NOT EXISTS oauth_app_clients_pk_ix ON oauth_app_clients (id);

CREATE UNIQUE INDEX IF NOT EXISTS oauth_cimd_clients_pk_ix ON oauth_cimd_clients (id);

CREATE UNIQUE INDEX IF NOT EXISTS oauth_tokens_pk_ix ON oauth_tokens (id);

CREATE UNIQUE INDEX IF NOT EXISTS observed_agent_events_pk_ix ON observed_agent_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_log_entries_pk_ix ON evidence_log_entries (log_id, seq);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_log_nodes_pk_ix ON evidence_log_nodes (log_id, level, index);

CREATE UNIQUE INDEX IF NOT EXISTS platform_measurement_changes_pk_ix ON platform_measurement_changes (id);

CREATE UNIQUE INDEX IF NOT EXISTS platform_settings_pk_ix ON platform_settings (id);

CREATE UNIQUE INDEX IF NOT EXISTS policy_bundle_events_pk_ix ON policy_bundle_events (id);

CREATE UNIQUE INDEX IF NOT EXISTS policy_bundles_pk_ix ON policy_bundles (id);

CREATE UNIQUE INDEX IF NOT EXISTS policy_templates_pk_ix ON policy_templates (id);

CREATE UNIQUE INDEX IF NOT EXISTS prompt_cache_policies_pk_ix ON prompt_cache_policies (id);

CREATE UNIQUE INDEX IF NOT EXISTS proxy_logs_pk_ix ON proxy_logs (id);

CREATE UNIQUE INDEX IF NOT EXISTS proxy_metrics_pk_ix ON proxy_metrics (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_agent_cards_pk_ix ON resource_group_agent_cards (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_agent_members_pk_ix ON resource_group_agent_members (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_agent_skills_pk_ix ON resource_group_agent_skills (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_argument_policies_pk_ix ON resource_group_argument_policies (resource_group_id, argument_policy_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_cache_configs_pk_ix ON resource_group_cache_configs (resource_group_id, cache_config_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_deployment_machines_pk_ix ON resource_group_deployment_machines (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_end_user_groups_pk_ix ON resource_group_end_user_groups (resource_group_id, end_user_group_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_guardrails_pk_ix ON resource_group_guardrails (resource_group_id, guardrail_config_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_kb_collections_pk_ix ON resource_group_kb_collections (resource_group_id, kb_collection_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_kb_connectors_pk_ix ON resource_group_kb_connectors (resource_group_id, kb_connector_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_llm_governance_pk_ix ON resource_group_llm_governance (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_llm_models_pk_ix ON resource_group_llm_models (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_mcp_capabilities_pk_ix ON resource_group_mcp_capabilities (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_memberships_pk_ix ON resource_group_memberships (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_prompt_cache_policies_pk_ix ON resource_group_prompt_cache_policies (resource_group_id, prompt_cache_policy_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_semantic_policies_pk_ix ON resource_group_semantic_policies (resource_group_id, semantic_policy_id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_group_server_configs_pk_ix ON resource_group_server_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS resource_groups_pk_ix ON resource_groups (id);

CREATE UNIQUE INDEX IF NOT EXISTS router_cache_configs_pk_ix ON router_cache_configs (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS run_interventions_pk_ix ON run_interventions (id);

CREATE UNIQUE INDEX IF NOT EXISTS saved_log_views_pk_ix ON saved_log_views (id);

CREATE UNIQUE INDEX IF NOT EXISTS scheduler_runtime_state_pk_ix ON scheduler_runtime_state (scheduler_name);

CREATE UNIQUE INDEX IF NOT EXISTS schema_version_pk_ix ON schema_version (id);

CREATE UNIQUE INDEX IF NOT EXISTS scim_tokens_pk_ix ON scim_tokens (id);

CREATE UNIQUE INDEX IF NOT EXISTS search_provider_configs_pk_ix ON search_provider_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS semantic_cache_configs_pk_ix ON semantic_cache_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS semantic_cache_tool_rules_pk_ix ON semantic_cache_tool_rules (id);

CREATE UNIQUE INDEX IF NOT EXISTS semantic_policies_pk_ix ON semantic_policies (id);

CREATE UNIQUE INDEX IF NOT EXISTS server_configs_pk_ix ON server_configs (id);

CREATE UNIQUE INDEX IF NOT EXISTS session_budget_resets_pk_ix ON session_budget_resets (tenant_id, session_id);

CREATE UNIQUE INDEX IF NOT EXISTS skill_sources_pk_ix ON skill_sources (id);

CREATE UNIQUE INDEX IF NOT EXISTS system_admin_audit_logs_pk_ix ON system_admin_audit_logs (id);

CREATE UNIQUE INDEX IF NOT EXISTS system_admin_sessions_pk_ix ON system_admin_sessions (id);

CREATE UNIQUE INDEX IF NOT EXISTS system_admins_pk_ix ON system_admins (id);

CREATE UNIQUE INDEX IF NOT EXISTS system_alerts_pk_ix ON system_alerts (id);

CREATE UNIQUE INDEX IF NOT EXISTS system_operation_log_pk_ix ON system_operation_log (id);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_a2a_keys_pk_ix ON tenant_a2a_keys (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_admin_audit_logs_pk_ix ON tenant_admin_audit_logs (id);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_admin_sessions_pk_ix ON tenant_admin_sessions (id);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_admins_pk_ix ON tenant_admins (id);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_role_permissions_pk_ix ON tenant_role_permissions (id);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_roles_pk_ix ON tenant_roles (id);

CREATE UNIQUE INDEX IF NOT EXISTS tenants_pk_ix ON tenants (id);

CREATE UNIQUE INDEX IF NOT EXISTS tool_approval_requests_pk_ix ON tool_approval_requests (id);

CREATE UNIQUE INDEX IF NOT EXISTS usage_alerts_pk_ix ON usage_alerts (id);

CREATE UNIQUE INDEX IF NOT EXISTS usage_import_checkpoints_pk_ix ON usage_import_checkpoints (provider_id);

CREATE UNIQUE INDEX IF NOT EXISTS usage_import_run_logs_pk_ix ON usage_import_run_logs (id);

CREATE UNIQUE INDEX IF NOT EXISTS usage_logs_pk_ix ON usage_logs (id);

CREATE UNIQUE INDEX IF NOT EXISTS video_generation_jobs_pk_ix ON video_generation_jobs (id);

CREATE UNIQUE INDEX IF NOT EXISTS virtual_servers_pk_ix ON virtual_servers (id);

CREATE UNIQUE INDEX IF NOT EXISTS witnesses_pk_ix ON witnesses (id);

CREATE UNIQUE INDEX IF NOT EXISTS ecs_deployments_task_arn_key_ix ON ecs_deployments (task_arn);

CREATE UNIQUE INDEX IF NOT EXISTS evidence_logs_tenant_id_key_ix ON evidence_logs (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS kb_connector_credentials_connector_id_key_ix ON kb_connector_credentials (connector_id);

CREATE UNIQUE INDEX IF NOT EXISTS llm_provider_catalog_key_key_ix ON llm_provider_catalog (key);

CREATE UNIQUE INDEX IF NOT EXISTS search_provider_configs_tenant_id_key_ix ON search_provider_configs (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_card_caps_name_ix ON agent_card_capabilities (agent_card_id, capability_name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_cards_tenant_name_ix ON agent_cards (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_identities_idp_subject_ix ON agent_identities (idp_server_id, idp_subject);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_identities_tenant_name_ix ON agent_identities (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_skill_assets_skill_filename_ix ON agent_skill_assets (skill_id, filename);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_skill_deps_tenant_parent_dependency_ix ON agent_skill_dependencies (tenant_id, parent_skill_id, dependency_skill_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_skill_resources_skill_filename_ix ON agent_skill_resources (skill_id, filename);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_skill_scripts_skill_filename_ix ON agent_skill_scripts (skill_id, filename);

CREATE UNIQUE INDEX IF NOT EXISTS uix_agent_skills_tenant_name_version_ix ON agent_skills (tenant_id, name, version);

CREATE UNIQUE INDEX IF NOT EXISTS uix_ai_assets_tenant_kind_ref_ix ON ai_assets (tenant_id, asset_kind, ref_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_audit_export_dest_tenant_name_ix ON audit_export_destinations (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_cache_tool_rule_ix ON semantic_cache_tool_rules (cache_config_id, server_config_id, capability_type, capability_name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_discovered_assets_tenant_key_ix ON discovered_assets (tenant_id, asset_key);

CREATE UNIQUE INDEX IF NOT EXISTS uix_docker_deployments_tenant_container_id_ix ON docker_deployments (tenant_id, container_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_docker_deployments_tenant_container_name_ix ON docker_deployments (tenant_id, container_name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_docker_deployments_tenant_name_ix ON docker_deployments (tenant_id, deployment_name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_docker_images_tenant_image_id_ix ON docker_images (tenant_id, image_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_end_user_groups_tenant_parent_name_ix ON resource_groups (tenant_id, parent_group_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_end_users_tenant_email_ix ON end_users (tenant_id, email);

CREATE UNIQUE INDEX IF NOT EXISTS uix_end_users_tenant_username_ix ON end_users (tenant_id, username);

CREATE UNIQUE INDEX IF NOT EXISTS uix_iso42001_coverage_tenant_control_ix ON iso42001_control_coverage (tenant_id, control_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_llm_catalog_provider_model_ix ON llm_catalog (provider_key, model_name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_oauth_app_clients_tenant_idp_client_ix ON oauth_app_clients (tenant_id, idp_id, client_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_observed_agent_events_tenant_event_ix ON observed_agent_events (tenant_id, event_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_resource_group_memberships_tenant_resouce_group_ix ON resource_group_memberships (tenant_id, user_id, group_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_rg_agent_member_ix ON resource_group_agent_members (tenant_id, agent_id, group_id);

CREATE UNIQUE INDEX IF NOT EXISTS uix_saved_log_views_tenant_scope_owner_name_ix ON saved_log_views (tenant_id, scope, owner_admin_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_system_admins_email_ix ON system_admins (email);

CREATE UNIQUE INDEX IF NOT EXISTS uix_system_admins_username_ix ON system_admins (username);

CREATE UNIQUE INDEX IF NOT EXISTS uix_tenant_admins_tenant_email_ix ON tenant_admins (tenant_id, email);

CREATE UNIQUE INDEX IF NOT EXISTS uix_tenant_admins_tenant_username_ix ON tenant_admins (tenant_id, username);

CREATE UNIQUE INDEX IF NOT EXISTS uix_tenant_role_permissions_role_permission_ix ON tenant_role_permissions (role_id, permission);

CREATE UNIQUE INDEX IF NOT EXISTS uix_tenant_roles_tenant_name_ix ON tenant_roles (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uix_tenants_domain_ix ON tenants (domain);

CREATE UNIQUE INDEX IF NOT EXISTS uix_tenants_name_ix ON tenants (name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_implementation_releases_key_ix ON agent_implementation_releases (tenant_id, agent_id, name, version, build);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_baseline_epochs_key_ix ON ai_system_baseline_epochs (tenant_id, ai_system_id, metric_key, basis, caller_system_id, bucket, starts_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_baselines_key_ix ON ai_system_baselines (tenant_id, ai_system_id, metric_key, bucket, actor_id, basis, caller_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_contracts_version_ix ON ai_system_contracts (tenant_id, ai_system_id, version);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_data_scopes_system_ix ON ai_system_data_scopes (tenant_id, ai_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_dependencies_system_card_ix ON ai_system_dependencies (tenant_id, ai_system_id, agent_card_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_dependencies_system_target_ix ON ai_system_dependencies (tenant_id, ai_system_id, depends_on_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_health_key_ix ON ai_system_health (tenant_id, ai_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_liveness_system_ix ON ai_system_liveness_expectations (tenant_id, ai_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_run_feedback_user_ix ON ai_system_run_feedback (tenant_id, root_task_id, user_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_run_scopes_key_ix ON ai_system_run_scopes (tenant_id, root_task_id, scope_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_run_segments_run_system_ix ON ai_system_run_segments (tenant_id, run_id, segment_system_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_runs_root_ix ON ai_system_runs (tenant_id, root_task_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_argument_policies_tenant_name_ix ON argument_policies (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_req_job_custom_id_ix ON batch_requests (batch_job_id, custom_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_end_user_groups_tenant_idp_external_ix ON end_user_groups (tenant_id, idp_server_id, source_external_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_end_user_groups_tenant_name_ix ON end_user_groups (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_agent_card_ix ON resource_group_agent_cards (resource_group_id, agent_card_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_agent_skill_ix ON resource_group_agent_skills (resource_group_id, agent_skill_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_deployment_machine_ix ON resource_group_deployment_machines (resource_group_id, machine_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_llm_governance_ix ON resource_group_llm_governance (resource_group_id, llm_model_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_llm_model_ix ON resource_group_llm_models (resource_group_id, llm_model_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_server_capability_ix ON resource_group_mcp_capabilities (resource_group_id, server_config_id, capability_type, capability_name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_group_server_config_ix ON resource_group_server_configs (resource_group_id, server_config_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_connectors_tenant_name_ix ON kb_connectors (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_identity_user_type_principal_ix ON kb_connector_identity_mappings (end_user_id, connector_type, source_principal);

CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_providers_tenant_key_ix ON llm_providers (tenant_id, key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_catalog_tenant_slug_ix ON mcp_catalog_entries (tenant_id, slug);

CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_cimd_clients_scope_ix ON oauth_cimd_clients (tenant_id, idp_id, cimd_url);

CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_tokens_tenant_idp_user_server_ix ON oauth_tokens (tenant_id, idp_id, idp_user_id, mcp_server_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_template_name_tenant_ix ON policy_templates (name, tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_cache_policies_tenant_name_ix ON prompt_cache_policies (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_replay_suites_name_ix ON ai_system_replay_suites (tenant_id, ai_system_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_response_policies_name_ix ON ai_system_response_policies (tenant_id, ai_system_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_run_interventions_run_ix ON run_interventions (tenant_id, root_task_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_server_config_tenant_server_client_name_ix ON server_configs (tenant_id, mcp_server_id, oauth_client_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_source_tenant_repo_ref_ix ON skill_sources (tenant_id, repo_url, git_ref);

CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_alert_per_period_ix ON usage_alerts (tenant_id, resource_type, group_id, metric, threshold_pct, period_start);

CREATE UNIQUE INDEX IF NOT EXISTS usage_logs_request_id_key_ix ON usage_logs (request_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_guardrail_configs_tenant_name ON guardrail_configs (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS ix_health_check_configs_tenant ON health_check_configs (tenant_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_kbc_tenant_name ON kb_collections (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS ix_llm_runner_machine_tenant_name ON llm_runner_machines (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS ix_mcp_catalog_global_slug ON mcp_catalog_entries (slug) WHERE (tenant_id IS NULL);

CREATE UNIQUE INDEX IF NOT EXISTS ix_scc_tenant_name ON semantic_cache_configs (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_achk_tenant_name ON assurance_checks (tenant_id, name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_action_records_log_seq ON action_records (log_id, seq);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_identity_approval_history_current ON agent_identity_approval_history (agent_id) WHERE (valid_to IS NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_contracts_one_active ON ai_system_contracts (tenant_id, ai_system_id) WHERE ((status)::text = 'active'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_system_lifecycle_events_subject ON ai_system_lifecycle_events (tenant_id, ai_system_id, event_type, subject_id) WHERE (subject_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_assurance_check_reviews_sample ON assurance_check_reviews (check_id, evaluator_fingerprint, sample_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_assurance_check_rubrics_period ON assurance_check_rubrics (check_id, rubric_digest, period_start);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ckr_check_version_run ON assurance_check_results (check_id, check_version, run_id) WHERE ((run_id IS NOT NULL) AND (NOT backtest));

CREATE UNIQUE INDEX IF NOT EXISTS uq_ckv_check_version ON assurance_check_versions (check_id, version);

CREATE UNIQUE INDEX IF NOT EXISTS uq_compliance_notice_configs_scope ON compliance_notice_configs (tenant_id, COALESCE(ai_system_id, ''::character varying));

CREATE UNIQUE INDEX IF NOT EXISTS uq_compliance_reports_period ON compliance_reports (tenant_id, framework_id, COALESCE(ai_system_id, ''::character varying), cadence, period_start);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dig_tenant_week ON assurance_digests (tenant_id, week_start);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_signing_keys_active ON evidence_signing_keys (tenant_id) WHERE ((status)::text = 'active'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_tree_heads_log_size ON evidence_tree_heads (log_id, tree_size);

CREATE UNIQUE INDEX IF NOT EXISTS uq_idp_servers_tenant_oidc_issuer ON idp_servers (tenant_id, issuer) WHERE ((protocol)::text = 'oidc'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_inbox_open_fingerprint ON assurance_inbox_items (tenant_id, fingerprint) WHERE ((state)::text <> 'resolved'::text);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_documents_connector_doc_key ON kb_documents (connector_doc_key) WHERE (connector_doc_key IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ndel_subscription_event ON notification_deliveries (subscription_id, inbox_event_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_bundles_active_per_scope ON policy_bundles (tenant_id, scope_group_id) WHERE (is_active = true);

CREATE UNIQUE INDEX IF NOT EXISTS uq_proxy_logs_chain_seq ON proxy_logs (chain_id, chain_seq) WHERE (chain_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_system_alerts_active_dedup ON system_alerts (dedup_key) WHERE ((status)::text = ANY ((ARRAY['active'::character varying, 'acknowledged'::character varying])::text[]));
