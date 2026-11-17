// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! How long a human-approval hold stays open — ONE resolver for every hold.
//!
//! Measured on a running demo system: 76 holds expired in
//! one day, every one after 300 s. Only the one tool whose capability filter
//! carried `approval_timeout_seconds` held for a day; skill runs, notes and
//! every hold raised while the system was demoted to `approval_required`
//! took a literal `300` written at five call sites. The approver cannot
//! decide overnight, so the platform was quietly turning "ask a human" into
//! "refuse unless a human answers within five minutes".
//!
//! ## Precedence (first that states a window wins)
//!
//! 1. **The policy that demanded the human**, when it names a window for this
//!    action: the MCP capability-filter row (`approval_timeout_seconds`, on
//!    an `approval_required` row or an `enabled` row lending its window to a
//!    band / agent-grant hold), the group-wide MCP default
//!    (`mcp_global_governance.approval_timeout_seconds`) when it is what
//!    held the tool, and the skill execution mode
//!    (`skill_global_governance.approval_timeout_seconds`, or the skill
//!    assignment's) when it is what holds the execution. The more specific statement wins in BOTH directions — an
//!    operator who sets one tool's window means that tool.
//! 2. **The Resource Group's default window**
//!    (`resource_groups.approval_window_seconds`), for the group the hold is
//!    routed to (the same group `tool_approval_requests.requester_group_id`
//!    records — its members are the approvers), **min-wins across the group
//!    and every ancestor**. A parent's window is a ceiling a child cannot
//!    lengthen: the house model for governance (limits and policies are
//!    restrictive and always inherited, not gated by `inherit_from_parent`),
//!    and the same merge every other approval timeout already uses (capability
//!    filter rows, skill governance, LLM tool-use governance). The control
//!    plane refuses a child window longer than the one it inherits, so the
//!    console never shows a value the runtime discards.
//! 3. **The tenant's default** (`tenants.default_approval_window_seconds`).
//! 4. **The platform default**, [`PLATFORM_DEFAULT_SECONDS`].
//!
//! Every value is clamped to `1 ..= MAX_SECONDS` (7 days): a typo must not
//! park a run for years.
//!
//! ## One place
//!
//! `agent_approval::require_or_enqueue` is the only writer of approval rows
//! and resolves the window itself from the context's
//! `policy_window_seconds`, the skill execution mode's hold included.
//! `tests/approval_hold_attribution_guard.rs` refuses a
//! window literal or a second default anywhere else.

use sqlx::PgPool;

use crate::models::auth::AuthContext;
use crate::models::db::ResourceGroup;

/// The window when neither a policy, a group nor the tenant states one.
pub const PLATFORM_DEFAULT_SECONDS: i32 = 300;

/// Ceiling on any resolved window (7 days). The control plane accepts
/// 60 s ..= this for the group and tenant defaults (database CHECKs).
pub const MAX_SECONDS: i32 = 7 * 24 * 3600;

/// Where the resolved window came from. Recorded on the hold
/// (`request_metadata.approval_window_source`) so an approver — and an
/// auditor — can see why a hold lasted as long as it did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowSource {
    Policy,
    ResourceGroup,
    Tenant,
    Platform,
}

impl WindowSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Policy => "policy",
            Self::ResourceGroup => "resource_group",
            Self::Tenant => "tenant",
            Self::Platform => "platform",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResolvedWindow {
    pub seconds: i32,
    pub source: WindowSource,
}

fn positive(v: Option<i32>) -> Option<i32> {
    v.filter(|s| *s > 0)
}

/// The pure precedence rule — see the module docs. `group_chain_min` is the
/// smallest positive window stated anywhere in the hold group's chain.
pub fn choose(
    policy_window: Option<i32>,
    group_chain_min: Option<i32>,
    tenant_default: Option<i32>,
) -> ResolvedWindow {
    let (seconds, source) = if let Some(s) = positive(policy_window) {
        (s, WindowSource::Policy)
    } else if let Some(s) = positive(group_chain_min) {
        (s, WindowSource::ResourceGroup)
    } else if let Some(s) = positive(tenant_default) {
        (s, WindowSource::Tenant)
    } else {
        (PLATFORM_DEFAULT_SECONDS, WindowSource::Platform)
    };
    ResolvedWindow {
        seconds: seconds.clamp(1, MAX_SECONDS),
        source,
    }
}

/// The group a hold is routed to: the same derivation
/// `tool_approval_requests.requester_group_id` records. Its members are the
/// approvers, so its window is the one that applies.
pub fn hold_group_id(auth: &AuthContext, groups: &[ResourceGroup]) -> Option<String> {
    auth.api_key_group_id
        .clone()
        .or_else(|| auth.resolved_group_id.clone())
        .or_else(|| groups.first().map(|g| g.id.clone()))
}

/// The group chain's min window and the tenant default, in one statement.
/// The chain walk is capped at the hierarchy's ten levels, so a cycle
/// cannot spin; `ancestor_chain_ids` in `usage` walks the same edges.
const WINDOW_SQL: &str = r#"
WITH RECURSIVE chain AS (
    SELECT id, parent_group_id, approval_window_seconds, 0 AS depth
      FROM resource_groups
     WHERE id = $2 AND tenant_id = $1
    UNION ALL
    SELECT rg.id, rg.parent_group_id, rg.approval_window_seconds, c.depth + 1
      FROM resource_groups rg
      JOIN chain c ON rg.id = c.parent_group_id
     WHERE rg.tenant_id = $1 AND c.depth < 10
)
SELECT (SELECT MIN(approval_window_seconds) FROM chain WHERE approval_window_seconds > 0),
       (SELECT default_approval_window_seconds FROM tenants WHERE id = $1)
"#;

/// Resolve the window of a hold raised for `hold_group` (see
/// [`hold_group_id`]) whose raising policy stated `policy_window` (or not).
///
/// A read failure is logged at ERROR and falls back to the tenant-less
/// platform default: a hold is still raised (the action waits for a human
/// either way), it is only shorter than configured — never an approval
/// that was not given.
pub async fn resolve(
    db: &PgPool,
    tenant_id: &str,
    hold_group: Option<&str>,
    policy_window: Option<i32>,
) -> ResolvedWindow {
    if positive(policy_window).is_some() {
        return choose(policy_window, None, None);
    }
    let row: Result<Option<(Option<i32>, Option<i32>)>, sqlx::Error> = sqlx::query_as(WINDOW_SQL)
        .bind(tenant_id)
        .bind(hold_group.unwrap_or(""))
        .fetch_optional(db)
        .await;
    match row {
        Ok(Some((group_min, tenant_default))) => choose(None, group_min, tenant_default),
        Ok(None) => choose(None, None, None),
        Err(e) => {
            tracing::error!(
                error = %e,
                tenant_id,
                group = hold_group.unwrap_or(""),
                "approval window lookup failed — using the platform default"
            );
            choose(None, None, None)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_policy_window_wins_in_both_directions() {
        assert_eq!(
            choose(Some(120), Some(86_400), Some(3600)),
            ResolvedWindow {
                seconds: 120,
                source: WindowSource::Policy
            }
        );
        assert_eq!(
            choose(Some(86_400), Some(600), None),
            ResolvedWindow {
                seconds: 86_400,
                source: WindowSource::Policy
            }
        );
    }

    #[test]
    fn the_group_chain_then_the_tenant_then_the_platform() {
        assert_eq!(
            choose(None, Some(86_400), Some(3600)),
            ResolvedWindow {
                seconds: 86_400,
                source: WindowSource::ResourceGroup
            }
        );
        assert_eq!(
            choose(None, None, Some(3600)),
            ResolvedWindow {
                seconds: 3600,
                source: WindowSource::Tenant
            }
        );
        assert_eq!(
            choose(None, None, None),
            ResolvedWindow {
                seconds: PLATFORM_DEFAULT_SECONDS,
                source: WindowSource::Platform
            }
        );
        assert_eq!(PLATFORM_DEFAULT_SECONDS, 300);
    }

    #[test]
    fn a_non_positive_value_states_nothing() {
        assert_eq!(
            choose(Some(0), Some(-5), Some(0)).source,
            WindowSource::Platform
        );
    }

    #[test]
    fn every_source_is_clamped_to_seven_days() {
        assert_eq!(choose(Some(i32::MAX), None, None).seconds, MAX_SECONDS);
        assert_eq!(choose(None, Some(i32::MAX), None).seconds, MAX_SECONDS);
        assert_eq!(choose(None, None, Some(i32::MAX)).seconds, MAX_SECONDS);
    }

    #[test]
    fn source_vocabulary_is_stable() {
        // Recorded on every hold (`request_metadata.approval_window_source`)
        // and read by the console; a rename is a wire change.
        let all = [
            WindowSource::Policy,
            WindowSource::ResourceGroup,
            WindowSource::Tenant,
            WindowSource::Platform,
        ]
        .map(WindowSource::as_str);
        assert_eq!(all, ["policy", "resource_group", "tenant", "platform"]);
    }
}
