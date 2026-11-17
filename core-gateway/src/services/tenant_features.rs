// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The tenant's feature switches that carry traffic.
//!
//! The Control Plane keeps a tenant's switches in `tenants.features`, a JSON
//! object keyed by feature: a missing key is on, `false` is off. Of its
//! features, these carry requests through this gateway, and a tenant whose
//! switch is off is refused on that surface:
//!
//! | Feature | Key | Refused at |
//! |---|---|---|
//! | Models | `models` | every model call (`llm_preflight::run_preflight`) |
//! | MCP servers | `mcp_servers` | a proxied MCP server (`routes::mcp`) |
//! | Virtual MCP servers | `virtual_mcp_servers` | `routes::vmcp` |
//! | Agent skills | `agent_skills` | a skill execution, on every surface (`skill_orchestrator::execute_skill`) |
//! | Knowledge base | `knowledge_base` | retrieval for a grounded request |
//! | Agents (A2A) | `a2a` | the A2A routes |
//!
//! The switches come with the tenant's status, which the tenant layer loads
//! and caches for every request (`tenant_cache`): asking costs no database
//! read while the status is cached. A value that is not `true` or `false`
//! is "off" (fail closed), and so is a `features` column that is not an
//! object or SQL NULL.

use serde_json::Value;

use crate::services::AppState;
use crate::services::tenant_cache::TenantStatus;

/// A feature that carries traffic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Feature {
    Models,
    McpServers,
    VirtualMcpServers,
    AgentSkills,
    KnowledgeBase,
    A2a,
}

impl Feature {
    pub const ALL: [Feature; 6] = [
        Feature::Models,
        Feature::McpServers,
        Feature::VirtualMcpServers,
        Feature::AgentSkills,
        Feature::KnowledgeBase,
        Feature::A2a,
    ];

    /// The key in `tenants.features`.
    pub fn key(self) -> &'static str {
        match self {
            Feature::Models => "models",
            Feature::McpServers => "mcp_servers",
            Feature::VirtualMcpServers => "virtual_mcp_servers",
            Feature::AgentSkills => "agent_skills",
            Feature::KnowledgeBase => "knowledge_base",
            Feature::A2a => "a2a",
        }
    }

    /// The refusal's sentence.
    pub fn disabled_message(self) -> &'static str {
        match self {
            Feature::Models => "Models feature is disabled for this tenant",
            Feature::McpServers => "MCP servers feature is disabled for this tenant",
            Feature::VirtualMcpServers => "Virtual MCP servers feature is disabled for this tenant",
            Feature::AgentSkills => "Agent skills feature is disabled for this tenant",
            Feature::KnowledgeBase => "Knowledge base feature is disabled for this tenant",
            Feature::A2a => "A2A feature is disabled for this tenant",
        }
    }
}

/// Which traffic-carrying features a tenant has switched off.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TenantFeatures {
    off: Vec<Feature>,
}

impl TenantFeatures {
    /// Every feature off: the switches of a tenant that does not exist.
    pub fn none() -> Self {
        Self {
            off: Feature::ALL.to_vec(),
        }
    }

    /// The switches as `tenants.features` states them.
    pub fn from_column(column: Option<&Value>) -> Self {
        match column {
            None | Some(Value::Null) => Self::default(),
            Some(Value::Object(map)) => Self {
                off: Feature::ALL
                    .into_iter()
                    .filter(|f| !matches!(map.get(f.key()), None | Some(Value::Bool(true))))
                    .collect(),
            },
            Some(_) => Self::none(),
        }
    }

    pub fn allows(&self, feature: Feature) -> bool {
        !self.off.contains(&feature)
    }
}

/// The tenant's status as the tenant layer reads it, `None` for a tenant
/// that does not exist. One query for the suspension and the switches.
pub async fn load_status(
    db: &sqlx::PgPool,
    tenant_id: &str,
) -> Result<Option<TenantStatus>, sqlx::Error> {
    let row: Option<(bool, Option<String>, Option<Value>)> =
        sqlx::query_as("SELECT suspended, suspension_reason, features FROM tenants WHERE id = $1")
            .bind(tenant_id)
            .fetch_optional(db)
            .await?;
    Ok(
        row.map(|(suspended, suspension_reason, features)| TenantStatus {
            suspended,
            suspension_reason,
            features: TenantFeatures::from_column(features.as_ref()),
        }),
    )
}

/// The tenant's switches: from the cached status the tenant layer loaded
/// for this request, or read (and cached) when it has expired since.
pub async fn of(state: &AppState, tenant_id: &str) -> Result<TenantFeatures, sqlx::Error> {
    if let Some(status) = state.tenant_cache.get_tenant(tenant_id) {
        return Ok(status.features);
    }
    Ok(match load_status(&state.db, tenant_id).await? {
        Some(status) => {
            let features = status.features.clone();
            state
                .tenant_cache
                .insert_tenant(tenant_id.to_string(), status);
            features
        }
        None => TenantFeatures::none(),
    })
}

/// `Ok(())` when the tenant's `feature` is on; otherwise the sentence of the
/// refusal (`Err(Some)`), or `Err(None)` when the switches could not be read
/// (the caller refuses as unavailable).
pub async fn require(
    state: &AppState,
    tenant_id: &str,
    feature: Feature,
) -> Result<(), Option<&'static str>> {
    match of(state, tenant_id).await {
        Ok(features) if features.allows(feature) => Ok(()),
        Ok(_) => Err(Some(feature.disabled_message())),
        Err(e) => {
            tracing::error!(error = %e, tenant_id, feature = feature.key(), "tenant features unreadable; refusing");
            Err(None)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_missing_key_is_on_and_false_is_off() {
        let f = TenantFeatures::from_column(Some(&json!({"models": false, "a2a": true})));
        assert!(!f.allows(Feature::Models));
        assert!(f.allows(Feature::A2a));
        assert!(f.allows(Feature::McpServers));
        assert!(TenantFeatures::from_column(None).allows(Feature::KnowledgeBase));
        assert!(TenantFeatures::from_column(Some(&Value::Null)).allows(Feature::AgentSkills));
    }

    #[test]
    fn a_value_that_is_not_a_switch_is_off() {
        let f = TenantFeatures::from_column(Some(&json!({"mcp_servers": "no", "models": 0})));
        assert!(!f.allows(Feature::McpServers));
        assert!(!f.allows(Feature::Models));
        assert!(f.allows(Feature::A2a));
        let f = TenantFeatures::from_column(Some(&json!(["models"])));
        assert!(Feature::ALL.iter().all(|x| !f.allows(*x)));
    }
}
