// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Generic icon serving endpoint with in-memory caching.
//!
//! Extracted into a config-driven core so the same
//! ETag + caching machinery serves icons for any DB entity that
//! adopts the five-column shape (`icon_data`, `icon_mime_type`,
//! `icon_size`, `icon_hash`, `icon_uploaded_at`):
//!
//!   * `mcp_servers`   — `/v1/assets/mcp-servers/{id}/icon`
//!   * `llm_providers` — `/v1/assets/llm-providers/{id}/icon`
//!   * future: `agent_skills`, `idp_servers`, …
//!
//! Adding a new entity is a 4-line change: register a new
//! `IconSource` constant and call
//! `Router::route("/{prefix}/{id}/icon", get(serve_icon_for(SOURCE)))`.
//!
//! Behaviour
//! ---------
//! - `Cache-Control: public, max-age=86400` (24h browser cache)
//! - `ETag` based on `icon_hash` (304 Not Modified on revalidation)
//! - In-memory LRU cache namespaced by `IconSource::cache_prefix`
//!   so two entity types with the same id (theoretical edge case)
//!   don't shadow each other.
//! - No authentication — icons are public assets, the same way
//!   provider wordmarks are public on the providers' own sites.

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;

use crate::services::SharedState;

/// Minimum photo capability-token length (hex chars). Mirrors the Python
/// `app/core/photo_token.py` rule so the `?h=` token is enforced identically
/// whether the browser hits the proxy (here) or the control plane.
const MIN_PHOTO_TOKEN_LEN: usize = 8;

/// True iff `token` is a valid capability token for the photo whose content
/// hash is `stored_hash`: a prefix of the hash, at least
/// [`MIN_PHOTO_TOKEN_LEN`] hex chars long. Profile photos are served without
/// auth so `<img>` tags work; requiring this `?h=` token (carried in the
/// stored `avatar_url`) stops unauthenticated enumeration of photos by
/// sequential integer id and cross-tenant scraping — you can't fetch a photo
/// unless you already know its hash.
fn photo_token_ok(stored_hash: &str, token: Option<&str>) -> bool {
    let token = match token {
        Some(t) => t,
        None => return false,
    };
    if token.len() < MIN_PHOTO_TOKEN_LEN || token.len() > stored_hash.len() {
        return false;
    }
    stored_hash.as_bytes()[..token.len()] == *token.as_bytes()
}

/// Static configuration describing one icon source.
///
/// `table` is the Postgres table name. The query expects the row
/// to have `icon_data`, `icon_mime_type`, `icon_hash` columns and
/// the primary-key `id` column. `cache_prefix` keys the in-memory
/// IconCache so different entity types don't collide.
#[derive(Clone, Copy)]
struct IconSource {
    table: &'static str,
    cache_prefix: &'static str,
    /// Fallback MIME used when a row's `icon_mime_type` is NULL —
    /// PNG matches the historic MCP behaviour, but we tune SVG
    /// for `llm_providers` since most provider icons are vector.
    default_mime: &'static str,
}

const MCP_SERVER_ICON: IconSource = IconSource {
    table: "mcp_servers",
    cache_prefix: "mcp",
    default_mime: "image/png",
};

const LLM_PROVIDER_ICON: IconSource = IconSource {
    table: "llm_providers",
    cache_prefix: "prov",
    default_mime: "image/svg+xml",
};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/mcp-servers/{server_id}/icon", get(get_mcp_icon))
        .route("/llm-providers/{provider_id}/icon", get(get_provider_icon))
        // End-user profile photos. Public (same as MCP icons —
        // photos display in shared chat history, multi-user
        // approvals, etc., where the viewer may not be the
        // photo's owner). The avatar_url stored on each user
        // points here, so both the portal-side photo upload
        // and the admin-side photo upload converge on a single
        // serving endpoint that the proxy actually owns.
        .route("/users/end-user/{user_id}/photo", get(get_end_user_photo))
}

// Per-source thin wrappers so axum can route to a concrete
// handler — closures-with-captured-config don't fit the
// `Router::route(path, get(fn))` signature without boxing.

async fn get_mcp_icon(
    state: State<SharedState>,
    path: Path<String>,
    headers: HeaderMap,
) -> Response {
    serve_entity_icon(state.0, path.0, headers, MCP_SERVER_ICON).await
}

async fn get_provider_icon(
    state: State<SharedState>,
    path: Path<String>,
    headers: HeaderMap,
) -> Response {
    serve_entity_icon(state.0, path.0, headers, LLM_PROVIDER_ICON).await
}

/// Serve an end-user's profile photo bytes.
///
/// The `end_users` table predates the canonical
/// `icon_*`-prefixed asset shape — it uses `photo_data`,
/// `photo_mime_type`, `photo_hash` instead. Rather than
/// generalise `IconSource` (which would force-rename a hot
/// table's columns or thread per-source SQL), we run a
/// dedicated query here. Behaviour matches the icon path:
/// in-memory cache (namespaced `end-user-photo`), ETag-based
/// 304 revalidation, 24h browser cache.
///
/// Public — same reasoning as MCP icons. A user's photo is
/// visible to anyone who can see the user (chat history,
/// approvals, audit logs); pre-signing every render would
/// be more friction than the bytes are worth.
async fn get_end_user_photo(
    State(state): State<SharedState>,
    Path(user_id): Path<String>,
    Query(params): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> Response {
    let token = params.get("h").map(String::as_str);
    let cache_key = format!("end-user-photo:{user_id}");

    if let Some((data, mime_type, etag)) = state.icon_cache.get(&cache_key) {
        // Capability check against the cached hash (etag == photo_hash)
        // before serving — keep the cache hot path gated too.
        if !photo_token_ok(&etag, token) {
            return StatusCode::NOT_FOUND.into_response();
        }
        if let Some(if_none_match) = headers.get(header::IF_NONE_MATCH) {
            if let Ok(val) = if_none_match.to_str() {
                if val.trim_matches('"') == etag || val == format!("\"{etag}\"") {
                    return (
                        StatusCode::NOT_MODIFIED,
                        [
                            (header::CACHE_CONTROL, "public, max-age=86400"),
                            (header::ETAG, &format!("\"{etag}\"")),
                        ],
                    )
                        .into_response();
                }
            }
        }
        return (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, mime_type.as_str()),
                (header::CACHE_CONTROL, "public, max-age=86400"),
                (header::ETAG, &format!("\"{etag}\"")),
            ],
            data,
        )
            .into_response();
    }

    // Parse the path segment as i32 — `end_users.id` is a serial
    // integer. Reject non-integer inputs early rather than
    // letting the query 500 on a type mismatch.
    let id: i32 = match user_id.parse() {
        Ok(n) => n,
        Err(_) => return StatusCode::NOT_FOUND.into_response(),
    };

    let row: Result<Option<(Vec<u8>, Option<String>, Option<String>)>, sqlx::Error> =
        sqlx::query_as(
            "SELECT photo_data, photo_mime_type, photo_hash \
         FROM end_users \
         WHERE id = $1 AND photo_data IS NOT NULL",
        )
        .bind(id)
        .fetch_optional(&state.db)
        .await;

    let (data, mime_type, hash) = match row {
        Ok(Some((d, m, h))) => (
            d,
            m.unwrap_or_else(|| "image/jpeg".to_string()),
            h.unwrap_or_default(),
        ),
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        // A failed read is a 503, not a missing icon.
        Err(e) => return crate::services::db_errors::unavailable("the image", &e),
    };

    // Capability check: require the `?h=` token (a prefix of photo_hash) so
    // this public route can't enumerate photos by integer id. 404 (not 403)
    // so we don't reveal which ids exist.
    if !photo_token_ok(&hash, token) {
        return StatusCode::NOT_FOUND.into_response();
    }

    if let Some(if_none_match) = headers.get(header::IF_NONE_MATCH) {
        if let Ok(val) = if_none_match.to_str() {
            if !hash.is_empty() && (val.trim_matches('"') == hash || val == format!("\"{hash}\"")) {
                state
                    .icon_cache
                    .insert(cache_key, data, mime_type, hash.clone());
                return (
                    StatusCode::NOT_MODIFIED,
                    [
                        (header::CACHE_CONTROL, "public, max-age=86400"),
                        (header::ETAG, &format!("\"{hash}\"")),
                    ],
                )
                    .into_response();
            }
        }
    }

    state
        .icon_cache
        .insert(cache_key, data.clone(), mime_type.clone(), hash.clone());

    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, mime_type.as_str()),
            (header::CACHE_CONTROL, "public, max-age=86400"),
            (header::ETAG, &format!("\"{hash}\"")),
        ],
        data,
    )
        .into_response()
}

/// Generic icon-serving core. Hot path is the in-memory cache;
/// DB roundtrip only on cache miss or eviction.
async fn serve_entity_icon(
    state: SharedState,
    entity_id: String,
    headers: HeaderMap,
    source: IconSource,
) -> Response {
    let cache_key = format!("{}:{entity_id}", source.cache_prefix);

    if let Some((data, mime_type, etag)) = state.icon_cache.get(&cache_key) {
        if let Some(if_none_match) = headers.get(header::IF_NONE_MATCH) {
            if let Ok(val) = if_none_match.to_str() {
                if val.trim_matches('"') == etag || val == format!("\"{etag}\"") {
                    return (
                        StatusCode::NOT_MODIFIED,
                        [
                            (header::CACHE_CONTROL, "public, max-age=86400"),
                            (header::ETAG, &format!("\"{etag}\"")),
                        ],
                    )
                        .into_response();
                }
            }
        }
        return (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, mime_type.as_str()),
                (header::CACHE_CONTROL, "public, max-age=86400"),
                (header::ETAG, &format!("\"{etag}\"")),
            ],
            data,
        )
            .into_response();
    }

    // Cache miss — query the DB.
    //
    // We construct the SQL by formatting the table name in (it's
    // a const, so this is safe) and parameterise only the id.
    let sql = format!(
        "SELECT icon_data, icon_mime_type, icon_hash \
         FROM {} \
         WHERE id = $1 AND icon_data IS NOT NULL",
        source.table,
    );
    let row: Result<Option<(Vec<u8>, Option<String>, Option<String>)>, sqlx::Error> =
        sqlx::query_as(&sql)
            .bind(&entity_id)
            .fetch_optional(&state.db)
            .await;

    let (data, mime_type, hash) = match row {
        Ok(Some((d, m, h))) => (
            d,
            m.unwrap_or_else(|| source.default_mime.into()),
            h.unwrap_or_default(),
        ),
        Ok(None) => return StatusCode::NOT_FOUND.into_response(),
        // A failed read is a 503, not a missing icon.
        Err(e) => return crate::services::db_errors::unavailable("the image", &e),
    };

    // 304 short-circuit before sending the body.
    if let Some(if_none_match) = headers.get(header::IF_NONE_MATCH) {
        if let Ok(val) = if_none_match.to_str() {
            if !hash.is_empty() && (val.trim_matches('"') == hash || val == format!("\"{hash}\"")) {
                state
                    .icon_cache
                    .insert(cache_key, data, mime_type, hash.clone());
                return (
                    StatusCode::NOT_MODIFIED,
                    [
                        (header::CACHE_CONTROL, "public, max-age=86400"),
                        (header::ETAG, &format!("\"{hash}\"")),
                    ],
                )
                    .into_response();
            }
        }
    }

    state
        .icon_cache
        .insert(cache_key, data.clone(), mime_type.clone(), hash.clone());

    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, mime_type.as_str()),
            (header::CACHE_CONTROL, "public, max-age=86400"),
            (header::ETAG, &format!("\"{hash}\"")),
        ],
        data,
    )
        .into_response()
}

#[cfg(test)]
mod photo_token_tests {
    use super::photo_token_ok;

    const HASH: &str = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";

    #[test]
    fn rejects_missing_token() {
        assert!(!photo_token_ok(HASH, None));
    }

    #[test]
    fn rejects_token_shorter_than_minimum() {
        // 7 chars < MIN_PHOTO_TOKEN_LEN (8), even though it's a real prefix.
        assert!(!photo_token_ok(HASH, Some("abcdef0")));
    }

    #[test]
    fn accepts_legacy_8char_prefix() {
        assert!(photo_token_ok(HASH, Some("abcdef01")));
    }

    #[test]
    fn accepts_full_hash() {
        assert!(photo_token_ok(HASH, Some(HASH)));
    }

    #[test]
    fn rejects_wrong_prefix() {
        assert!(!photo_token_ok(HASH, Some("ffffffff")));
    }

    #[test]
    fn rejects_token_longer_than_hash() {
        assert!(!photo_token_ok("abcdef01", Some(HASH)));
    }

    #[test]
    fn rejects_empty_stored_hash() {
        assert!(!photo_token_ok("", Some("abcdef01")));
    }
}
