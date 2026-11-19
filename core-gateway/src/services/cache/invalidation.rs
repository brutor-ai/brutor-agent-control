// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What the gateway does when the control plane says a resource changed: a
//! list of named handlers ([`BUILTIN_INVALIDATION_HANDLERS`]), each answering
//! one or more resource types, and the registry the listener consults
//! ([`InvalidationRegistry`]).
//!
//! They were the arms of one `match` in
//! [`CacheInvalidator`](super::CacheInvalidator). As a list they can be named,
//! tagged with the Enterprise Edition feature they belong to, and extended by
//! another distribution. Each function below is the body of the arm
//! that stood in the `match`, unchanged, with one exception: the guardrail arm
//! is two handlers, because its last statement drops the response cache, which
//! is an Enterprise feature, and the rest of it is not. (The
//! handlers of a feature that is an extension are registered by the
//! extension, from its own module: prompt caching, the response cache,
//! the knowledge base. What is left here is the Community Edition's.)
//!
//! How a message is answered:
//!
//! - every handler registered for the message's type runs, in registration
//!   order (the built-in ones first);
//! - a type with no handler gets [`unregistered_type`], which was the `_` arm.
//!
//! A handler can be added for a type that already has one. None can be removed
//! or replaced: what the gateway evicts on a change is not something an
//! extension may take away.

use std::future::Future;
use std::pin::Pin;

use super::{CacheInvalidator, InvalidationMessage, ResourceCache};

/// What a handler returns: the work of one invalidation.
pub type HandlerFuture<'a> = Pin<Box<dyn Future<Output = ()> + Send + 'a>>;

/// Answers one invalidation message.
pub type HandleFn = for<'a> fn(&'a CacheInvalidator, &'a InvalidationMessage) -> HandlerFuture<'a>;

/// Drops everything a handler keeps fresh, after the listener (re)subscribes.
pub type FlushFn = for<'a> fn(&'a CacheInvalidator) -> HandlerFuture<'a>;

/// One handler.
#[derive(Clone, Copy)]
pub struct InvalidationHandler {
    /// Unique, stable name.
    pub name: &'static str,
    /// The `type` values of the messages it answers.
    pub resource_types: &'static [&'static str],
    /// The Enterprise feature the handler belongs to, or `None` for a handler
    /// of the Community Edition.
    pub feature: Option<&'static str>,
    /// Evicts what the message makes stale.
    pub handle: HandleFn,
    /// Messages published while the listener was not subscribed are gone, so
    /// after every (re)subscribe each cache that depends on them is dropped
    /// whole ([`CacheInvalidator::flush_config_caches`]). A handler whose
    /// cache is not in that function's own list names its flush here.
    pub flush: Option<FlushFn>,
}

/// Why a handler was not registered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InvalidationRegistryError {
    /// The name is empty or already taken.
    Name(String),
    /// It answers no resource type, or an empty one.
    ResourceTypes(String),
}

impl std::fmt::Display for InvalidationRegistryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Name(name) => write!(
                f,
                "invalidation handler name {name:?} is empty or already registered"
            ),
            Self::ResourceTypes(name) => write!(
                f,
                "invalidation handler {name:?} names no resource type, or an empty one"
            ),
        }
    }
}

impl std::error::Error for InvalidationRegistryError {}

/// The handlers a listener consults, in registration order.
#[derive(Clone)]
pub struct InvalidationRegistry {
    handlers: Vec<InvalidationHandler>,
}

impl InvalidationRegistry {
    /// The built-in handlers.
    pub fn builtin() -> Self {
        Self {
            handlers: BUILTIN_INVALIDATION_HANDLERS.to_vec(),
        }
    }

    /// Add a handler after the ones already registered. Its resource types
    /// may be ones that already have a handler: it then runs after them.
    pub fn register(
        &mut self,
        handler: InvalidationHandler,
    ) -> Result<(), InvalidationRegistryError> {
        if handler.name.is_empty() || self.handlers.iter().any(|h| h.name == handler.name) {
            return Err(InvalidationRegistryError::Name(handler.name.to_string()));
        }
        if handler.resource_types.is_empty() || handler.resource_types.iter().any(|t| t.is_empty())
        {
            return Err(InvalidationRegistryError::ResourceTypes(
                handler.name.to_string(),
            ));
        }
        self.handlers.push(handler);
        Ok(())
    }

    /// Every handler, in registration order.
    pub fn handlers(&self) -> &[InvalidationHandler] {
        &self.handlers
    }

    /// The handlers that answer `resource_type`, in registration order. The
    /// match is exact: `Model` and `model ` are not `model`.
    pub fn handlers_for<'r>(
        &'r self,
        resource_type: &'r str,
    ) -> impl Iterator<Item = &'r InvalidationHandler> + 'r {
        self.handlers
            .iter()
            .filter(move |h| h.resource_types.contains(&resource_type))
    }
}

/// Every built-in handler, in the order the arms stood in.
pub const BUILTIN_INVALIDATION_HANDLERS: &[InvalidationHandler] = &[
    InvalidationHandler {
        name: "model_or_routing_group",
        resource_types: &["model", "routing_group"],
        feature: None,
        handle: model_or_routing_group,
        flush: None,
    },
    InvalidationHandler {
        name: "llm_provider",
        resource_types: &["llm_provider", "provider"],
        feature: None,
        handle: llm_provider,
        flush: None,
    },
    InvalidationHandler {
        name: "mcp_server",
        resource_types: &["mcp_server", "server_config"],
        feature: None,
        handle: mcp_server,
        flush: None,
    },
    InvalidationHandler {
        name: "agent_skill",
        resource_types: &["skill", "agent_skill"],
        feature: None,
        handle: agent_skill,
        flush: None,
    },
    InvalidationHandler {
        name: "resource_group",
        resource_types: &["resource_group"],
        feature: None,
        handle: resource_group,
        flush: None,
    },
    InvalidationHandler {
        name: "ai_system_dependency",
        resource_types: &["ai_system_dependency"],
        feature: None,
        handle: ai_system_dependency,
        flush: None,
    },
    InvalidationHandler {
        name: "argument_policy",
        resource_types: &["argument_policy"],
        feature: None,
        handle: argument_policy,
        flush: None,
    },
    InvalidationHandler {
        name: "guardrail",
        resource_types: &["guardrail", "guardrail_config"],
        feature: None,
        handle: guardrail,
        flush: None,
    },
    InvalidationHandler {
        name: "agent_card",
        resource_types: &["agent_card"],
        feature: None,
        handle: agent_card,
        flush: None,
    },
    InvalidationHandler {
        name: "semantic_policy",
        resource_types: &["semantic_policy"],
        feature: None,
        handle: semantic_policy,
        flush: None,
    },
    InvalidationHandler {
        name: "membership",
        resource_types: &["membership", "assign"],
        feature: None,
        handle: membership,
        flush: None,
    },
];

fn model_or_routing_group<'a>(
    inv: &'a CacheInvalidator,
    msg: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async move {
        inv.model_cache.invalidate(&msg.tenant_id, &msg.id).await;
        // Model changes can affect group effective model lists
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn llm_provider<'a>(inv: &'a CacheInvalidator, msg: &'a InvalidationMessage) -> HandlerFuture<'a> {
    Box::pin(async move {
        // Provider config changed (rename, enable toggle, etc).
        // The provider cache holds slug + URL + auth defaults
        // for adapter dispatch — drop the entry and the next
        // model lookup will repopulate from DB. Also invalidate
        // model entries since their cached `provider` slug is
        // stamped at load time from this cache.
        inv.provider_cache.invalidate(&msg.tenant_id, &msg.id).await;
        inv.model_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn mcp_server<'a>(inv: &'a CacheInvalidator, msg: &'a InvalidationMessage) -> HandlerFuture<'a> {
    Box::pin(async move {
        inv.mcp_cache.invalidate(&msg.tenant_id, &msg.id).await;
        // MCP changes can affect group effective MCP lists
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn agent_skill<'a>(inv: &'a CacheInvalidator, msg: &'a InvalidationMessage) -> HandlerFuture<'a> {
    Box::pin(async move {
        inv.skill_cache.invalidate(&msg.tenant_id, &msg.id).await;
        // Skill changes can affect group effective skill lists
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn resource_group<'a>(
    inv: &'a CacheInvalidator,
    msg: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async move {
        // A resource-group change — direct membership,
        // EndUserGroup bindings/membership, limits, governance —
        // can alter both the materialised group view AND the
        // user→groups mapping. Drop the tenant's group caches
        // (including `user_groups`) so EndUserGroup-mediated
        // access reflects immediately, matching how every other
        // resource type already invalidates here.
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
        // The tenant's identity policy default is published as a change of
        // the tenant's groups (it is a floor of every one of them) and is
        // read from the tenant's cached status: drop it, so the next call
        // reads the new default instead of the one cached before.
        if let Some(ref state) = inv.state {
            state.tenant_cache.invalidate_tenant(&msg.tenant_id);
        }
        // The run-ceiling cache reads `operating_envelope`
        // off this same row. Its own doc said it was "called when the
        // control plane signals a config change", and nothing called
        // it: a tightened run ceiling waited out the 60 s TTL while the
        // console showed the new one. `invalidate_all` rather than
        // per-group because the message carries the changed group, and
        // a ceiling can also move by editing an ANCESTOR whose id is
        // not in the message; the cache is small and refills on demand.
        crate::services::run_budget::invalidate_all();
    })
}

fn ai_system_dependency<'a>(
    inv: &'a CacheInvalidator,
    msg: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async move {
        // A declared dependency edge changed. Edges decide
        // which AI System an agent card is (the accepted-caller gate's
        // `card_service_system`), and that map lives with the tenant's
        // group entries.
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn argument_policy<'a>(
    inv: &'a CacheInvalidator,
    msg: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async move {
        // POLICY-10 AST-aware argument policies. Drop the
        // tenant's whole entry set — same conservative
        // strategy guardrails use — so the next request
        // cold-loads the updated rules.
        inv.argument_policy_cache
            .invalidate_tenant(&msg.tenant_id)
            .await;
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn guardrail<'a>(inv: &'a CacheInvalidator, msg: &'a InvalidationMessage) -> HandlerFuture<'a> {
    Box::pin(async move {
        // Guardrail rules are cached at three levels:
        //   1. `GuardrailsCache` — the per-(tenant, user|group)
        //      flat list that `apply_guardrails` consults.
        //   2. `EffectiveGroupData` only references skill/MCP/
        //      model IDs, not guardrails, but a guardrail change
        //      can flip whether a request is allowed at all,
        //      so we drop the materialised group view too —
        //      cheap (next request rebuilds from cache hits)
        //      and prevents subtle TOCTOU.
        //   3. The semantic response cache: `guardrail_cached_responses`,
        //      which answers the same message after this handler.
        inv.guardrails_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn agent_card<'a>(inv: &'a CacheInvalidator, msg: &'a InvalidationMessage) -> HandlerFuture<'a> {
    Box::pin(async move {
        // The card row + its capabilities are cached together
        // in `AgentCardCache`. Group bindings (which cards a
        // group can invoke) live in `EffectiveGroupData`, so
        // a card add/remove also drops the materialised group.
        inv.agent_card_cache
            .invalidate(&msg.tenant_id, &msg.id)
            .await;
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

fn semantic_policy<'a>(
    inv: &'a CacheInvalidator,
    msg: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async move {
        // The arm this message never had.
        //
        // The control plane publishes on every semantic-policy write,
        // exactly as it does for guardrails and argument policies, but
        // the proxy had no `semantic_policy` case: the message arrived
        // and fell through to the catch-all, which does not touch this
        // cache. So an operator who tightened or disabled a policy kept
        // enforcing the old one for the rest of the TTL, with the UI
        // showing the new one. Reached through `state` because the
        // cache lives on `SharedState`.
        if let Some(ref state) = inv.state {
            state
                .semantic_policy_cache
                .invalidate_tenant(&msg.tenant_id)
                .await;
        } else {
            tracing::debug!(
                tenant_id = %msg.tenant_id,
                "semantic_policy invalidation received without SharedState; \
                 the cache will clear on its own TTL"
            );
        }
    })
}

fn membership<'a>(inv: &'a CacheInvalidator, msg: &'a InvalidationMessage) -> HandlerFuture<'a> {
    Box::pin(async move {
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.model_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.mcp_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.skill_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}

/// A resource type no handler is registered for: the tenant's model, group,
/// MCP server, skill, guardrail and agent-card entries go. It was the `_` arm.
/// It is broad, and it still does not reach every cache, which is why a type
/// the control plane publishes needs a handler of its own
/// (`tests/llm_surface_evidence_guard.rs`).
pub(super) fn unregistered_type<'a>(
    inv: &'a CacheInvalidator,
    msg: &'a InvalidationMessage,
) -> HandlerFuture<'a> {
    Box::pin(async move {
        tracing::debug!(
            resource_type = %msg.resource_type,
            "Unknown invalidation type, invalidating all caches for tenant"
        );
        inv.model_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.group_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.mcp_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.skill_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.guardrails_cache.invalidate_tenant(&msg.tenant_id).await;
        inv.agent_card_cache.invalidate_tenant(&msg.tenant_id).await;
    })
}
