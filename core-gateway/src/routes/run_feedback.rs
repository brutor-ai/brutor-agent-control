// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run feedback — the run ledger's only true-outcome signal.
//!
//! Mounted under `/v1/runs` next to the run lifecycle routes:
//! a rating is about a run, not about the portal. The rater still
//! needs a portal session — the only human credential the gateway issues
//! today — so in a build without the portal nobody can rate, which is the
//! intended Community shape until another human credential exists.
//!
//! Everything else the platform records is *behaviour*: how many steps, which
//! tools, what it cost, whether the calls succeeded. None of it says whether
//! the answer was any good. `ai_system_runs.feedback_score` is commented "the
//! only true-outcome signal we will ever have" — and until now it had a read
//! path and no writer, so the column was a promise the product did not keep.
//!
//! ## How a rating finds its run
//!
//! The gateway returns `X-Brutor-Run-Id` on every LLM response (see
//! `constants::headers::X_BRUTOR_RUN_ID`). The client keeps it against the
//! message it rendered and sends it back here. The server therefore never has
//! to infer which run a rating refers to from timing — which would be wrong
//! for any user with two tabs open, and unfixable for a shared assistant.
//!
//! ## Why this writes to a table and not straight to the column
//!
//! A run does not exist in `ai_system_runs` until it is *finalized*, and
//! portal chats are finalized by the idle sweeper — 120 seconds after the last
//! call. Users click thumbs in about two seconds. Writing directly to the
//! column would therefore miss the row for essentially every real rating and
//! answer `404` to a user who did nothing wrong.
//!
//! So the rating lands in `ai_system_run_feedback` immediately, and the column
//! becomes a rollup of it: the finalizer reads the sum on every rollup (see
//! `UPSERT_SQL`), and this handler also updates the row directly for runs that
//! have already closed. Whichever way the two race, the answer is the same
//! because both read the same source of truth.
//!
//! ## Why ownership is checked against `proxy_logs`
//!
//! For the same timing reason — `ai_system_runs` may not exist yet — and for a
//! better one: `proxy_logs.gateway_user_id` is who actually made the calls,
//! whereas `ai_system_runs.actor_id` is a *derived* principal that prefers an
//! agent identity when one is present (see `derive_actor`). A portal user
//! driving an agentic AI System would fail an `actor_id` check despite being
//! the very person who read the answer.
//!
//! The check is not optional. A run id travels in headers, logs and traces; if
//! any authenticated user could rate any run, feedback would be a tenant-wide
//! write primitive and one user could quietly poison another AI System's
//! quality signal — which feeds health.
//!
//! ## Scores
//!
//! `+1` / `-1` — thumbs. Deliberately not a 1–5 scale: a five-point scale
//! invites averaging, and an average of a sparse, self-selected sample is a
//! number that looks like a measurement without being one. Thumbs stay
//! countable ("14 up, 3 down of 402 runs"), which is the honest shape.

use axum::{
    Json, Router,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
};
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;

use std::sync::Arc;

use crate::middleware::tenant::TenantContext;
use crate::services::AppState;

type SharedState = Arc<AppState>;

/// Free-text is capped. The field exists to make a thumbs-down actionable,
/// not to accept an essay — and an uncapped text column reachable by any
/// authenticated user is a storage-exhaustion primitive.
pub const MAX_COMMENT_CHARS: usize = 2000;

pub fn router() -> Router<SharedState> {
    Router::new().route("/{run_id}/feedback", post(submit_feedback))
}

#[derive(Debug, Deserialize)]
pub struct FeedbackRequest {
    /// `1` thumbs up, `-1` thumbs down, `0` clears a prior rating. A user who
    /// mis-clicks must be able to take it back: a rating they did not mean is
    /// worse than no rating at all.
    pub score: i32,
    /// Optional "what was wrong with it".
    #[serde(default)]
    pub comment: Option<String>,
}

/// The accepted vocabulary. A function rather than an inline match so the
/// check and the error message describing it cannot drift apart.
// Clippy suggests `-1..=1`. Equivalent today, and wrong as documentation:
// these are three named outcomes (down / withdraw / up), not a numeric range
// that happens to span three values. The enumeration is what makes it obvious
// that adding a fourth would be a vocabulary change, not a bounds tweak.
#[allow(clippy::manual_range_patterns)]
pub fn is_valid_score(score: i32) -> bool {
    matches!(score, -1 | 0 | 1)
}

/// POST /v1/runs/{run_id}/feedback
async fn submit_feedback(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(run_id): Path<String>,
    Json(body): Json<FeedbackRequest>,
) -> Response {
    let client_ip = client_addr.ip();
    let (tenant_id, user_id, _username) =
        match crate::routes::request::extract_portal_user(&state, &headers, &tenant, client_ip)
            .await
        {
            Ok(v) => v,
            Err(r) => return r,
        };

    if !is_valid_score(body.score) {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({ "error": "score must be 1 (up), -1 (down) or 0 (clear)" })),
        )
            .into_response();
    }

    let comment = body
        .comment
        .as_deref()
        .map(str::trim)
        .filter(|c| !c.is_empty());
    if let Some(c) = comment {
        if c.chars().count() > MAX_COMMENT_CHARS {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "error": format!("comment must be at most {MAX_COMMENT_CHARS} characters"),
                })),
            )
                .into_response();
        }
    }

    // Ownership. Both halves matter: the tenant check alone would let any
    // colleague rate any run in the organisation.
    //
    // 404 rather than 403 for a run that exists but is not the caller's —
    // "this run exists" is itself a fact about someone else's traffic.
    let participated = sqlx::query_scalar::<_, bool>(
        r#"
        SELECT EXISTS (
            SELECT 1 FROM proxy_logs
             WHERE tenant_id = $1
               AND root_task_id = $2
               AND gateway_user_id = $3
        )
        "#,
    )
    .bind(&tenant_id)
    .bind(&run_id)
    .bind(&user_id)
    .fetch_one(&state.db)
    .await;

    match participated {
        Ok(true) => {}
        Ok(false) => {
            return (
                StatusCode::NOT_FOUND,
                Json(json!({ "error": "Run not found" })),
            )
                .into_response();
        }
        Err(e) => {
            tracing::error!(error = %e, run_id = %run_id, "feedback: ownership check failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": "Could not record feedback" })),
            )
                .into_response();
        }
    }

    // 0 deletes rather than storing a zero. NULL/absent already means "not
    // rated"; a stored 0 would be a third state every consumer has to special
    // -case, and it would sum into the rollup as a rating nobody gave.
    let write = if body.score == 0 {
        sqlx::query(
            r#"
            DELETE FROM ai_system_run_feedback
             WHERE tenant_id = $1 AND root_task_id = $2 AND user_id = $3
            "#,
        )
        .bind(&tenant_id)
        .bind(&run_id)
        .bind(&user_id)
        .execute(&state.db)
        .await
    } else {
        sqlx::query(
            r#"
            INSERT INTO ai_system_run_feedback
                (id, tenant_id, root_task_id, user_id, score, comment)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (tenant_id, root_task_id, user_id) DO UPDATE SET
                score = EXCLUDED.score,
                comment = EXCLUDED.comment,
                updated_at = now()
            "#,
        )
        .bind(Uuid::new_v4().to_string())
        .bind(&tenant_id)
        .bind(&run_id)
        .bind(&user_id)
        .bind(body.score as i16)
        .bind(comment)
        .execute(&state.db)
        .await
    };

    if let Err(e) = write {
        tracing::error!(error = %e, run_id = %run_id, "feedback: write failed");
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": "Could not record feedback" })),
        )
            .into_response();
    }

    // Roll up into the run if it has already finalized. If it has not, this
    // touches nothing and the finalizer reads the same sum when it closes the
    // run — see UPSERT_SQL in run_ledger.rs.
    let rolled = sqlx::query(
        r#"
        UPDATE ai_system_runs r
           SET feedback_score = (
                   SELECT SUM(f.score)::smallint
                     FROM ai_system_run_feedback f
                    WHERE f.tenant_id = r.tenant_id
                      AND f.root_task_id = r.root_task_id)
         WHERE r.tenant_id = $1 AND r.root_task_id = $2
        "#,
    )
    .bind(&tenant_id)
    .bind(&run_id)
    .execute(&state.db)
    .await;

    // A failed rollup is not a failed rating: the rating is committed, and the
    // next finalization recomputes the column from it. WARN, not ERROR — the
    // user's action succeeded and nobody needs waking.
    if let Err(e) = rolled {
        tracing::warn!(error = %e, run_id = %run_id, "feedback: rollup deferred to finalizer");
    }

    tracing::info!(run_id = %run_id, score = body.score, "run feedback recorded");
    Json(json!({
        "run_id": run_id,
        "score": if body.score == 0 { None } else { Some(body.score) },
    }))
    .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_thumbs_and_clear() {
        assert!(is_valid_score(1));
        assert!(is_valid_score(-1));
        assert!(is_valid_score(0));
    }

    #[test]
    fn rejects_a_star_rating() {
        // The trap this guards: someone "improves" the portal to 1–5 stars,
        // the column quietly accepts 4, and every count of thumbs-up is wrong
        // without anything having failed.
        for score in [2, 3, 4, 5, -2, 100, i32::MIN, i32::MAX] {
            assert!(!is_valid_score(score), "{score} must be rejected");
        }
    }

    #[test]
    fn score_fits_smallint() {
        // The column is SMALLINT. Validation runs before the cast, so the
        // cast can never truncate — pinned because reordering them would be
        // an easy and silent mistake.
        for score in [-1i32, 0, 1] {
            assert_eq!(score as i16 as i32, score);
        }
    }

    /// Mirrors the handler's normalisation so the rule can be asserted
    /// without a live database.
    fn normalize(comment: Option<&str>) -> Option<&str> {
        comment.map(str::trim).filter(|c| !c.is_empty())
    }

    #[test]
    fn blank_comment_is_none_not_empty_string() {
        // An empty string in the column reads as "they left a comment" to
        // every triage query. Absence has to stay absence.
        assert_eq!(normalize(Some("")), None);
        assert_eq!(normalize(Some("   ")), None);
        assert_eq!(normalize(Some("  wrong answer  ")), Some("wrong answer"));
        assert_eq!(normalize(None), None);
    }

    #[test]
    fn comment_cap_counts_chars_not_bytes() {
        // A byte cap would reject a legitimate comment in any non-Latin
        // script at roughly a third of the advertised length.
        let emoji = "🙂".repeat(MAX_COMMENT_CHARS);
        assert_eq!(emoji.chars().count(), MAX_COMMENT_CHARS);
        assert!(
            emoji.len() > MAX_COMMENT_CHARS,
            "byte length far exceeds cap"
        );
    }
}
