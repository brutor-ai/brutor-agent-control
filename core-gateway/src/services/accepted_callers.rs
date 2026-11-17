// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Accepted-caller enforcement for shared services.
//!
//! A shared service is an AI System other systems call over A2A. It declares
//! who may call it (`resource_groups.intended_client_systems`)).
//! Without enforcement that declaration only feeds the advisory finding
//! `composition.unaccepted_caller`. With `enforce_accepted_callers` on, the
//! gateway refuses a call from a caller system that is not listed, BEFORE
//! the service is contacted (EU AI Act Art 25(4): the serving owner accepts
//! its callers).
//!
//! ## Resolution — the same rules the rest of the platform uses
//!
//! * **Service**: the target agent card resolves to an AI System through a
//!   declared `shared_service` edge anywhere in the tenant — the control
//!   plane's rule (`CompositionIndex.card_system`), served from the group
//!   cache ([`ResourceGroupCache::card_service_system`]). A card no edge
//!   names is not a Brutor service: nothing to enforce.
//! * **Enforcement config**: the service's own resource-group row
//!   (`enforce_accepted_callers`, `intended_client_systems`), read from the
//!   cached effective group — invalidated with every other RG field on a
//!   `resource_group` change (Redis pub/sub or the `brutor:cache:invalidate`
//!   NOTIFY). Per service, never inherited.
//! * **Caller**: the resource group the step is attributed to (the group the
//!   proxy_logs row carries) walked to its nearest AI System —
//!   [`crate::services::ai_system::nearest_ai_systems`], the resolver the run
//!   ledger uses to cut segments and to fill `caller_system_id`.
//!
//! ## Decisions
//!
//! | case                                                 | verdict |
//! |------------------------------------------------------|---------|
//! | card names no service, or the service does not enforce | not applicable |
//! | caller system is the service itself                  | accepted (a system calling its own card is not crossing a boundary) |
//! | caller system is listed                              | accepted |
//! | caller system is not listed                          | **refused** |
//! | caller is in no AI System                            | **refused** — the service accepts only the systems it listed; an unattributed caller is by definition not one of them |
//! | the edge lookup failed                               | **unavailable** — the route fails closed (`engine_failure`) |
//!
//! A refusal is proxy subtype [`proxy_subtype::UNACCEPTED_CALLER`], HTTP 403,
//! sealed `denied` by the evidence writer, and a `blocked` run segment.

use serde_json::Value;

use crate::constants::proxy_subtype;
use crate::services::SharedState;
use crate::services::cache::ResourceGroupCache;

/// Why a call was refused. `message` is the client-facing text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub service_system_id: String,
    pub caller_system_id: Option<String>,
    pub message: String,
}

impl Refusal {
    pub fn subtype(&self) -> &'static str {
        proxy_subtype::UNACCEPTED_CALLER
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// The target is not an enforcing Brutor service.
    NotApplicable,
    Accepted,
    Refused(Refusal),
    /// The configuration could not be read; the caller fails closed.
    Unavailable(String),
}

/// Message on a fail-closed refusal (subtype `engine_failure`).
pub const UNAVAILABLE_MESSAGE: &str = "accepted_caller_check_unavailable";

/// The ids in an `intended_client_systems` JSON value. Anything that is not
/// a list of strings accepts nobody — never "accept everyone".
pub fn accepted_systems(v: Option<&Value>) -> Vec<&str> {
    v.and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

/// The pure decision, once service and caller are resolved.
pub fn decide(
    service_system_id: &str,
    enforce: bool,
    intended: Option<&Value>,
    caller_system_id: Option<&str>,
) -> Verdict {
    if !enforce {
        return Verdict::NotApplicable;
    }
    match caller_system_id {
        Some(c) if c == service_system_id => Verdict::Accepted,
        Some(c) if accepted_systems(intended).contains(&c) => Verdict::Accepted,
        Some(c) => Verdict::Refused(Refusal {
            service_system_id: service_system_id.to_string(),
            caller_system_id: Some(c.to_string()),
            message: format!(
                "unaccepted_caller: AI System '{c}' is not an accepted caller of the \
                 service '{service_system_id}'"
            ),
        }),
        None => Verdict::Refused(Refusal {
            service_system_id: service_system_id.to_string(),
            caller_system_id: None,
            message: format!(
                "unaccepted_caller: the caller belongs to no AI System, and the service \
                 '{service_system_id}' accepts only the AI Systems it lists"
            ),
        }),
    }
}

/// Resolve and decide for one A2A call to `card_id`.
///
/// `caller_group_id` is the group the call's proxy_logs row is attributed
/// to. Reads only cached configuration until enforcement is known to apply;
/// the caller's nearest-system walk runs only for an enforcing service.
pub async fn check(
    state: &SharedState,
    tenant_id: &str,
    caller_group_id: Option<&str>,
    card_id: &str,
) -> Verdict {
    check_with(
        &state.group_cache,
        &state.db,
        tenant_id,
        caller_group_id,
        card_id,
    )
    .await
}

pub async fn check_with(
    cache: &ResourceGroupCache,
    db: &sqlx::PgPool,
    tenant_id: &str,
    caller_group_id: Option<&str>,
    card_id: &str,
) -> Verdict {
    let service = match cache.card_service_system(tenant_id, card_id).await {
        Ok(Some(s)) => s,
        Ok(None) => return Verdict::NotApplicable,
        Err(e) => {
            tracing::error!(
                tenant_id,
                card_id,
                error = %e,
                "accepted-caller check: dependency edge lookup failed; failing closed"
            );
            return Verdict::Unavailable(e.to_string());
        }
    };
    let svc = match cache.try_get_effective_group(tenant_id, &service).await {
        Ok(Some(svc)) => svc,
        // Inactive or deleted service group: it enforces nothing.
        Ok(None) => return Verdict::NotApplicable,
        // An unreadable service group is not an absent one. Reading
        // it as "enforces nothing" let every caller through on a DB error.
        Err(e) => {
            tracing::error!(
                tenant_id,
                card_id,
                error = %e,
                "accepted-caller check: service group load failed; failing closed"
            );
            return Verdict::Unavailable(e.to_string());
        }
    };
    if !svc.group.enforce_accepted_callers {
        return Verdict::NotApplicable;
    }
    let caller = match caller_group_id.map(str::trim).filter(|g| !g.is_empty()) {
        // The run ledger's resolver (`run_ledger` builds the segments'
        // `group_system` map with it), so the gate's caller and a segment's
        // `caller_system_id` are one computation.
        Some(g) => {
            match crate::services::ai_system::nearest_ai_systems(db, tenant_id, &[g.to_string()])
                .await
            {
                Ok(mut found) => found.remove(g).map(|(id, _)| id),
                // Not "an unknown caller": the gate cannot decide.
                Err(e) => {
                    tracing::error!(error = %e, card_id, "accepted-caller check: caller system lookup failed; failing closed");
                    return Verdict::Unavailable(e.to_string());
                }
            }
        }
        None => None,
    };
    decide(
        &service,
        true,
        svc.group.intended_client_systems.as_ref(),
        caller.as_deref(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn enforcement_off_never_refuses() {
        assert_eq!(
            decide("svc", false, Some(&json!([])), Some("stranger")),
            Verdict::NotApplicable
        );
        assert_eq!(decide("svc", false, None, None), Verdict::NotApplicable);
    }

    #[test]
    fn a_listed_caller_is_accepted_and_an_unlisted_one_refused() {
        let listed = json!(["sys-root", "sys-other"]);
        assert_eq!(
            decide("svc", true, Some(&listed), Some("sys-root")),
            Verdict::Accepted
        );
        match decide("svc", true, Some(&listed), Some("sys-stranger")) {
            Verdict::Refused(r) => {
                assert_eq!(r.service_system_id, "svc");
                assert_eq!(r.caller_system_id.as_deref(), Some("sys-stranger"));
                assert_eq!(r.subtype(), "unaccepted_caller");
                assert!(r.message.starts_with("unaccepted_caller:"));
            }
            v => panic!("an unlisted caller must be refused, got {v:?}"),
        }
    }

    #[test]
    fn a_caller_in_no_ai_system_is_refused() {
        match decide("svc", true, Some(&json!(["sys-root"])), None) {
            Verdict::Refused(r) => assert!(r.caller_system_id.is_none()),
            v => panic!("an unattributed caller must be refused, got {v:?}"),
        }
    }

    #[test]
    fn the_service_itself_is_always_accepted() {
        assert_eq!(decide("svc", true, None, Some("svc")), Verdict::Accepted);
    }

    #[test]
    fn a_malformed_list_accepts_nobody() {
        for bad in [
            json!("sys-root"),
            json!({"sys-root": true}),
            json!([1, 2]),
            Value::Null,
        ] {
            assert!(
                matches!(
                    decide("svc", true, Some(&bad), Some("sys-root")),
                    Verdict::Refused(_)
                ),
                "{bad} must not read as accepting sys-root"
            );
        }
        assert!(matches!(
            decide("svc", true, None, Some("sys-root")),
            Verdict::Refused(_)
        ));
    }
}
