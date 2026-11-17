// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A seeded [`CacheInvalidator`] for tests of what an invalidation message
//! evicts.
//!
//! The dispatch tests of `cache/mod.rs` were built on a rig that installed
//! the prompt cache, the response cache and the knowledge base. Those are
//! extensions of the Enterprise Edition, so the rig is split: this module is
//! the part both editions share — the invalidator, the caches of the
//! Community gateway seeded for two tenants, and what a message evicted from
//! them. The Community tests use it with the built-in handlers; the
//! Enterprise workspace uses it with the handlers and the state of the full
//! composition and adds what its own caches lost.
//!
//! Compiled for this crate's unit tests and, for another workspace's tests,
//! under the `db-tests` feature (the feature every test run of both
//! workspaces enables). Never part of a release build.

use std::sync::Arc;

use sqlx::postgres::PgPoolOptions;

use super::{
    AgentCardCache, AgentSkillCache, CacheInvalidator, InvalidationHandler, InvalidationMessage,
    InvalidationRegistry, McpServerCache, ModelCache, ProviderCache, ResourceGroupCache,
};
use crate::services::SharedState;
use crate::services::guardrails::GuardrailsCache;

/// An invalidator and handles to the caches it shares, seeded.
pub struct Rig {
    pub inv: CacheInvalidator,
    pub model: ModelCache,
    pub group: ResourceGroupCache,
    pub mcp: McpServerCache,
    pub skill: AgentSkillCache,
    pub provider: Arc<ProviderCache>,
    pub card: AgentCardCache,
    pub guardrails: GuardrailsCache,
}

/// A state as `AppState::new` leaves it: a Community gateway's, nothing
/// installed on it. Its pool never connects and gives up at once, so a
/// handler that reads the database through the state fails fast instead of
/// waiting for a server that is not there. An edition that composes
/// extensions installs them on the value before sharing it.
pub fn bare_state() -> crate::services::AppState {
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(std::time::Duration::from_millis(50))
        .connect_lazy("postgres://test:test@127.0.0.1:1/test")
        .expect("connect_lazy never fails on a syntactically-valid URL");
    crate::services::AppState::new(
        crate::config::AppConfig::for_test("postgres://test:test@127.0.0.1:1/test".into()),
        pool,
        None,
        crate::services::outbound_policy::client_builder()
            .build()
            .expect("an outbound client"),
        crate::services::telemetry::TelemetryState {
            prometheus_handle: None,
        },
    )
}

/// A fully-wired [`CacheInvalidator`] backed by a never-connecting lazy
/// pool, over `state` and consulting `handlers`. Every sub-cache is freshly
/// built; the invalidator and the caller share Clone-handles to each (the
/// sharing pattern the builder uses with `state.X.clone()`). The two
/// encryption services are handed in: each is a key derivation, which is
/// most of what a fixture costs.
pub fn build_invalidator(
    encryption: crate::services::encryption::EncryptionService,
    guardrails_encryption: crate::services::encryption::EncryptionService,
    state: SharedState,
    handlers: InvalidationRegistry,
) -> Rig {
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .connect_lazy("postgres://test:test@localhost:5432/test")
        .expect("connect_lazy never fails on a syntactically-valid URL");

    let provider = Arc::new(ProviderCache::new(pool.clone()));
    let model = ModelCache::new(pool.clone(), encryption, provider.clone());
    let group = ResourceGroupCache::new(pool.clone());
    let mcp = McpServerCache::new(pool.clone());
    let skill = AgentSkillCache::new(pool.clone());
    let card = AgentCardCache::new(pool.clone());
    let guardrails = GuardrailsCache::new(60, guardrails_encryption);

    let inv = CacheInvalidator {
        model_cache: model.clone(),
        group_cache: group.clone(),
        mcp_cache: mcp.clone(),
        skill_cache: skill.clone(),
        provider_cache: provider.clone(),
        guardrails_cache: guardrails.clone(),
        argument_policy_cache: crate::services::argument_policy::ArgumentPolicyCache::new(60),
        agent_card_cache: card.clone(),
        // The state an extension's handlers reach their caches through.
        // Its own resource caches are not the ones above; nothing here
        // reads them.
        state: Some(state),
        redis: None,
        handlers,
    };
    Rig {
        inv,
        model,
        group,
        mcp,
        skill,
        provider,
        card,
        guardrails,
    }
}

/// The two encryption services of a rig, derived once per process.
pub fn test_keys() -> (
    crate::services::encryption::EncryptionService,
    crate::services::encryption::EncryptionService,
) {
    static KEYS: std::sync::OnceLock<(
        crate::services::encryption::EncryptionService,
        crate::services::encryption::EncryptionService,
    )> = std::sync::OnceLock::new();
    KEYS.get_or_init(|| {
        (
            crate::services::encryption::EncryptionService::new("test-encryption-key-32-chars-ok!"),
            crate::services::encryption::EncryptionService::new("test-key"),
        )
    })
    .clone()
}

/// One entry of every kind of the Community gateway under the id `x1`, for
/// `tenant-a` and for `tenant-b`, on an invalidator over `state` that
/// consults `handlers` and then `extra`.
pub async fn seeded_rig(
    state: SharedState,
    handlers: InvalidationRegistry,
    extra: &[InvalidationHandler],
) -> Rig {
    let (encryption, guardrails_encryption) = test_keys();
    let mut rig = build_invalidator(encryption, guardrails_encryption, state, handlers);
    for handler in extra {
        rig.inv
            .handlers
            .register(*handler)
            .expect("the test's handler registers");
    }
    for tenant in ["tenant-a", "tenant-b"] {
        rig.model.insert_model_for_test(tenant, "x1").await;
        rig.provider.insert_provider_for_test(tenant, "x1").await;
        rig.mcp.insert_server_for_test(tenant, "x1").await;
        rig.skill.insert_skill_for_test(tenant, "x1").await;
        rig.card.insert_card_for_test(tenant, "x1", "billing").await;
        rig.group.insert_group_for_test(tenant, "x1").await;
        rig.guardrails.cache.write().await.insert(
            format!("{tenant}:__all__"),
            crate::services::guardrails::CacheEntry {
                configs: vec![],
                loaded_at: std::time::Instant::now(),
            },
        );
        rig.inv
            .argument_policy_cache
            .inner_write_for_test()
            .await
            .insert(
                format!("{tenant}:x1:mcp_input"),
                crate::services::argument_policy::cache::test_only_make_entry(vec![]),
            );
    }
    rig
}

pub async fn guardrails_contains(cache: &GuardrailsCache, key: &str) -> bool {
    cache.cache.read().await.contains_key(key)
}

/// Which of a tenant's seeded Community entries are gone, as names in a
/// fixed order.
pub async fn gone(rig: &Rig, tenant: &str) -> Vec<&'static str> {
    let mut out = Vec::new();
    if rig.model._peek_model(tenant, "x1").await.is_none() {
        out.push("model");
    }
    if rig.provider._peek(tenant, "x1").await.is_none() {
        out.push("provider");
    }
    if rig.group._peek_group(tenant, "x1").await.is_none() {
        out.push("group");
    }
    if rig.mcp._peek_server(tenant, "x1").await.is_none() {
        out.push("mcp");
    }
    if rig.skill._peek(tenant, "x1").await.is_none() {
        out.push("skill");
    }
    if rig.card._peek_by_id(tenant, "x1").await.is_none() {
        out.push("card");
    }
    if !guardrails_contains(&rig.guardrails, &format!("{tenant}:__all__")).await {
        out.push("guardrails");
    }
    if !rig
        .inv
        .argument_policy_cache
        .inner_write_for_test()
        .await
        .contains_key(&format!("{tenant}:x1:mcp_input"))
    {
        out.push("argument_policy");
    }
    out
}

/// Send one `update` message for `tenant-a` to the rig's invalidator.
pub async fn send(rig: &Rig, resource_type: &str, id: &str) {
    rig.inv
        .handle_invalidation(&InvalidationMessage {
            resource_type: resource_type.into(),
            action: "update".into(),
            tenant_id: "tenant-a".into(),
            id: id.into(),
        })
        .await;
}

/// What one message for `tenant-a` evicted from the Community caches of
/// this rig. Panics if anything of `tenant-b` is gone: no message crosses
/// tenants.
pub async fn evicted_on(rig: &Rig, resource_type: &str, id: &str) -> Vec<&'static str> {
    send(rig, resource_type, id).await;
    let evicted = gone(rig, "tenant-a").await;
    let other_tenant = gone(rig, "tenant-b").await;
    assert!(
        other_tenant.is_empty(),
        "{resource_type}: evicted tenant-b's {other_tenant:?} on a tenant-a message"
    );
    evicted
}

/// `name: feature: resource types`, in registration order.
pub fn described(registry: &InvalidationRegistry) -> Vec<String> {
    registry
        .handlers()
        .iter()
        .map(|h| {
            format!(
                "{}: {}: {}",
                h.name,
                h.feature.unwrap_or("-"),
                h.resource_types.join(" ")
            )
        })
        .collect()
}

/// The built-in handlers: what a gateway composed with no extension
/// consults. A handler moving between the editions, or answering another
/// type, is a decision: it has to be made here as well.
/// (a feature's handlers left this list in the step that made the feature an
/// extension; the Enterprise workspace records its extensions' handlers.)
pub const RECORDED_HANDLERS: &[&str] = &[
    "model_or_routing_group: -: model routing_group",
    "llm_provider: -: llm_provider provider",
    "mcp_server: -: mcp_server server_config",
    "agent_skill: -: skill agent_skill",
    "resource_group: -: resource_group",
    "ai_system_dependency: -: ai_system_dependency",
    "argument_policy: -: argument_policy",
    "guardrail: -: guardrail guardrail_config",
    "agent_card: -: agent_card",
    "semantic_policy: -: semantic_policy",
    "membership: -: membership assign",
];

/// `(resource type, evicted when the id matches, evicted when it does not)`
/// for every type a built-in handler answers, and one nobody registered.
///
/// One row is recorded as found: `semantic_policy` reaches through `state`
/// and evicts nothing this table observes (the state's semantic-policy
/// cache, which is not the fixture's); it is covered by its own tests.
pub const DISPATCH_TABLE: &[(&str, &str, &str)] = &[
    ("model", "model group", "group"),
    ("routing_group", "model group", "group"),
    ("llm_provider", "model provider", "model"),
    ("provider", "model provider", "model"),
    ("mcp_server", "group mcp", "group"),
    ("server_config", "group mcp", "group"),
    ("skill", "group skill", "group"),
    ("agent_skill", "group skill", "group"),
    ("resource_group", "group", "group"),
    ("ai_system_dependency", "group", "group"),
    (
        "argument_policy",
        "group argument_policy",
        "group argument_policy",
    ),
    ("guardrail", "group guardrails", "group guardrails"),
    ("guardrail_config", "group guardrails", "group guardrails"),
    ("agent_card", "group card", "group"),
    ("semantic_policy", "", ""),
    (
        "membership",
        "model group mcp skill",
        "model group mcp skill",
    ),
    ("assign", "model group mcp skill", "model group mcp skill"),
    // Not a type the dispatcher names: everything of the tenant's that
    // the catch-all knows goes.
    ("a_type_nobody_registered", BROAD_EVICTION, BROAD_EVICTION),
];

/// What a message of a type no handler answers evicts.
pub const BROAD_EVICTION: &str = "model group mcp skill card guardrails";
