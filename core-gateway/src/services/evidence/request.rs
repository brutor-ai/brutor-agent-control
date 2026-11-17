// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Request-scoped evidence facts.
//!
//! Three facts are discovered during a request and needed on the row the
//! request eventually writes, on whichever surface writes it:
//!
//! * the portal's **conversation handle** (`X-Brutor-Conversation-Handle`),
//!   validated as a ULID and stored clear in `custom_metadata` so an
//!   inference record can cite that conversation's notice record;
//! * the **approval request** this call enqueued, so the `awaiting_human`
//!   record names it and the later outcome — or the sweeper's `expired`
//!   record — can chain `supersedes` to it;
//! * the proxy-log **id reserved at dispatch** for a two-phase action, so
//!   the `planned` record sealed before the upstream call and the outcome
//!   record sealed after it share one `action_id`.
//!
//! Same mechanism and rationale as the task-locals in
//! `middleware::run_end`: recorded at the source, read by
//! `proxy_logger::build_proxy_log`, and a silent no-op outside a request
//! scope (background work, unit tests) — a proxy must not fall over
//! recording an evidence detail.

use axum::http::HeaderMap;
use serde_json::{Map, Value, json};
use std::cell::RefCell;
use std::future::Future;

#[derive(Debug, Default)]
pub struct RequestEvidence {
    conversation_handle: Option<String>,
    enqueued_approval: Option<String>,
    /// `(proxy_type, id)` — the id the outcome row of this proxy type takes.
    reserved_log_id: Option<(String, String)>,
    /// Upstream attempts whose outcome is unknown: the request was
    /// sent and then timed out or failed in a way that does not say whether
    /// the side effect ran or the generation was billed.
    ambiguous_upstream: Vec<String>,
}

/// At most this many ambiguous attempts are listed on one row.
const MAX_AMBIGUOUS: usize = 8;

tokio::task_local! {
    static CTX: RefCell<RequestEvidence>;
}

/// A conversation handle is a ULID and nothing else: never a session id, a
/// user id or anything derived from identity (it is stored in the clear). Any
/// other value is dropped rather than stored — an unvalidated header is a
/// channel for exactly the identifiers the record must never carry.
/// Returns the canonical (uppercase Crockford) spelling.
pub fn parse_conversation_handle(raw: &str) -> Option<String> {
    let t = raw.trim();
    if t.len() != 26 {
        return None;
    }
    ulid::Ulid::from_string(t).ok().map(|u| u.to_string())
}

pub fn handle_from_headers(headers: &HeaderMap) -> Option<String> {
    headers
        .get(crate::constants::headers::X_BRUTOR_CONVERSATION_HANDLE)
        .and_then(|v| v.to_str().ok())
        .and_then(parse_conversation_handle)
}

/// Run `fut` with a fresh evidence scope. Installed by
/// `middleware::run_end::RunEndLayer` around every handler, seeded with
/// [`handle_from_headers`].
pub async fn scope<F: Future>(conversation_handle: Option<String>, fut: F) -> F::Output {
    let ctx = RequestEvidence {
        conversation_handle,
        ..Default::default()
    };
    CTX.scope(RefCell::new(ctx), fut).await
}

/// The validated conversation handle of the current request, if any.
pub fn conversation_handle() -> Option<String> {
    CTX.try_with(|c| c.borrow().conversation_handle.clone())
        .ok()
        .flatten()
}

/// Record the approval request this call enqueued (first writer wins).
pub fn record_enqueued_approval(request_id: &str) {
    let _ = CTX.try_with(|c| {
        let mut c = c.borrow_mut();
        if c.enqueued_approval.is_none() {
            c.enqueued_approval = Some(request_id.to_owned());
        }
    });
}

/// Record an upstream attempt whose outcome is unknown: it reached
/// the upstream (or may have), then timed out or failed ambiguously, so the
/// action may have run and the provider may have billed for it. Lands in
/// the row's `custom_metadata.ambiguous_upstream`. `what` is a short fixed
/// description (a surface and a status), never identity or content.
pub fn record_ambiguous_upstream(what: &str) {
    let _ = CTX.try_with(|c| {
        let mut c = c.borrow_mut();
        if c.ambiguous_upstream.len() < MAX_AMBIGUOUS {
            c.ambiguous_upstream.push(what.to_owned());
        }
    });
}

pub fn ambiguous_upstream() -> Vec<String> {
    CTX.try_with(|c| c.borrow().ambiguous_upstream.clone())
        .unwrap_or_default()
}

pub fn enqueued_approval() -> Option<String> {
    CTX.try_with(|c| c.borrow().enqueued_approval.clone())
        .ok()
        .flatten()
}

/// Reserve the proxy-log id the outcome row of `proxy_type` will carry.
/// Idempotent within a request (a retried dispatch keeps the same action).
/// `None` outside a request scope — the caller then skips the planned
/// record, because without a shared id the outcome could not confirm it.
pub fn reserve_log_id(proxy_type: &str) -> Option<String> {
    CTX.try_with(|c| {
        let mut c = c.borrow_mut();
        if let Some((pt, id)) = &c.reserved_log_id
            && pt == proxy_type
        {
            return id.clone();
        }
        let id = ulid::Ulid::new().to_string();
        c.reserved_log_id = Some((proxy_type.to_owned(), id.clone()));
        id
    })
    .ok()
}

/// Consume the reserved id for a row of `proxy_type` (the next row of that
/// type built in this request is the outcome row).
pub fn take_reserved_log_id(proxy_type: &str) -> Option<String> {
    CTX.try_with(|c| {
        let mut c = c.borrow_mut();
        match &c.reserved_log_id {
            Some((pt, _)) if pt == proxy_type => c.reserved_log_id.take().map(|(_, id)| id),
            _ => None,
        }
    })
    .ok()
    .flatten()
}

/// The `custom_metadata` a row built in this request starts with. Only
/// clear-safe facts: an opaque conversation handle and an approval id.
pub fn row_metadata() -> Option<Value> {
    let (handle, approval, ambiguous) = CTX
        .try_with(|c| {
            let c = c.borrow();
            (
                c.conversation_handle.clone(),
                c.enqueued_approval.clone(),
                c.ambiguous_upstream.clone(),
            )
        })
        .ok()?;
    let mut m = Map::new();
    if let Some(h) = handle {
        m.insert("conversation_handle".into(), json!(h));
    }
    if let Some(a) = approval {
        m.insert("approval_request_id".into(), json!(a));
    }
    if !ambiguous.is_empty() {
        m.insert("ambiguous_upstream".into(), json!(ambiguous));
    }
    (!m.is_empty()).then_some(Value::Object(m))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_ulid_is_a_conversation_handle() {
        let u = ulid::Ulid::new().to_string();
        assert_eq!(parse_conversation_handle(&u).as_deref(), Some(u.as_str()));
        // lowercase is the same ULID, stored canonically
        assert_eq!(
            parse_conversation_handle(&u.to_lowercase()).as_deref(),
            Some(u.as_str())
        );
        for bad in [
            "",
            "user-42",
            "session:abc",
            "alice@example.com",
            "01ARZ3NDEKTSV4RRFFQ69G5FA",   // 25 chars
            "01ARZ3NDEKTSV4RRFFQ69G5FAVV", // 27 chars
            "01ARZ3NDEKTSV4RRFFQ69G5FA!",  // not Crockford
            "00000000-0000-0000-0000-000000000000",
        ] {
            assert_eq!(parse_conversation_handle(bad), None, "{bad}");
        }
    }

    #[test]
    fn outside_a_scope_everything_is_a_no_op() {
        record_enqueued_approval("apr-1");
        assert_eq!(enqueued_approval(), None);
        assert_eq!(conversation_handle(), None);
        assert_eq!(reserve_log_id("mcp"), None);
        assert_eq!(take_reserved_log_id("mcp"), None);
        assert_eq!(row_metadata(), None);
    }

    #[tokio::test]
    async fn scoped_facts_reach_the_row_metadata() {
        let handle = ulid::Ulid::new().to_string();
        let mut headers = HeaderMap::new();
        headers.insert(
            crate::constants::headers::X_BRUTOR_CONVERSATION_HANDLE,
            handle.parse().unwrap(),
        );
        scope(handle_from_headers(&headers), async {
            assert_eq!(conversation_handle().as_deref(), Some(handle.as_str()));
            record_enqueued_approval("apr-1");
            record_enqueued_approval("apr-2");
            assert_eq!(enqueued_approval().as_deref(), Some("apr-1"));
            assert_eq!(
                row_metadata(),
                Some(json!({"conversation_handle": handle, "approval_request_id": "apr-1"}))
            );
        })
        .await;
    }

    #[tokio::test]
    async fn a_reserved_id_goes_to_the_first_row_of_its_type_only() {
        scope(None, async {
            let id = reserve_log_id("mcp").expect("in scope");
            assert_eq!(reserve_log_id("mcp").as_deref(), Some(id.as_str()));
            assert_eq!(take_reserved_log_id("llm"), None, "other surface");
            assert_eq!(take_reserved_log_id("mcp").as_deref(), Some(id.as_str()));
            assert_eq!(take_reserved_log_id("mcp"), None, "consumed once");
        })
        .await;
    }

    #[tokio::test]
    async fn an_invalid_handle_header_is_dropped() {
        let mut headers = HeaderMap::new();
        headers.insert(
            crate::constants::headers::X_BRUTOR_CONVERSATION_HANDLE,
            "user-42".parse().unwrap(),
        );
        scope(handle_from_headers(&headers), async {
            assert_eq!(conversation_handle(), None);
            assert_eq!(row_metadata(), None);
        })
        .await;
    }
}
