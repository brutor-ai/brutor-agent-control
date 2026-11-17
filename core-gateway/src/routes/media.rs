// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! `GET /v1/media/{key}?expires=…&signature=…`: the signed download links of
//! the filesystem media store (`services::media_storage::filesystem`), served
//! by the gateway itself. The same contract the S3 store's presigned URLs
//! give a client: a URL from `GET /v1/proxy/files/{id}/url` that needs no
//! credential, names one object, and stops working at its expiry.
//!
//! - The link is the credential: no tenant is resolved (`TenantRule::Open`);
//!   the tenant is the key's first segment and the signature covers it.
//! - Unknown, expired, tampered or malformed links, a key that is not of
//!   the store's layout (path traversal included) and a file that is not
//!   there all answer the same 404, with no body that tells them apart. A
//!   directory is never listed: only a key that names a file parses.
//! - `Content-Type` is the one recorded at write time. Images (not SVG),
//!   audio and video are `inline` so that `<img>` / `<video>` render them;
//!   everything else is an `attachment`. `nosniff` and a `sandbox` CSP keep
//!   a stored file from running as a page of the gateway's origin.
//! - Answers 404 for every link when the store in use is not the filesystem
//!   one (S3 links go to the bucket).

use axum::{
    Router,
    body::Body,
    extract::{Path, Query, State},
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::get,
};
use serde::Deserialize;

use crate::services::SharedState;

pub fn router() -> Router<SharedState> {
    Router::new().route("/{*key}", get(download))
}

#[derive(Debug, Deserialize)]
pub(crate) struct LinkQuery {
    expires: Option<String>,
    signature: Option<String>,
}

/// The body of a download in the OpenAPI document: the file's bytes, of
/// whatever type it was stored with (`type: string, format: binary`). A
/// schema only: the body is streamed, so there is no value of this type.
enum MediaFile {}

impl utoipa::PartialSchema for MediaFile {
    fn schema() -> utoipa::openapi::RefOr<utoipa::openapi::schema::Schema> {
        use utoipa::openapi::schema::{KnownFormat, ObjectBuilder, SchemaFormat, Type};
        ObjectBuilder::new()
            .schema_type(Type::String)
            .format(Some(SchemaFormat::KnownFormat(KnownFormat::Binary)))
            .into()
    }
}

impl utoipa::ToSchema for MediaFile {}

#[utoipa::path(
    get,
    path = "/v1/media/{key}",
    tag = "Media",
    operation_id = "download_media",
    summary = "Download a file through a signed link",
    description = "Serves one stored file through a link the gateway signed. Clients do not \
                   build these links: `GET /v1/proxy/files/{file_id}/url` returns one (when the \
                   gateway's media store is the filesystem one; with the S3 store it returns a \
                   presigned bucket URL instead), and the client fetches it as it is, path and \
                   query unchanged. The link is opaque.\n\n\
                   No credential is sent: the link is the credential. It names one file, is \
                   bound to that file's tenant, and stops working at `expires`; changing any \
                   part of it invalidates it.\n\n\
                   Every link that does not open a file (unsigned, tampered, expired, \
                   malformed, a key outside the store, a file that is gone, or a gateway whose \
                   media store is not the filesystem one) answers the same `404` with no body \
                   that tells the cases apart.",
    params(
        ("key" = String, Path, description = "The stored file's key, as the link carries it \
                                              (several path segments)."),
        ("expires" = String, Query, description = "When the link stops working, in Unix \
                                                   seconds, as the link carries it."),
        ("signature" = String, Query, description = "The gateway's signature over the link, \
                                                     as the link carries it."),
    ),
    responses(
        (status = 200,
         description = "The file. `Content-Type` is the type recorded when the file was \
                        stored. Images (except SVG), audio and video are served `inline` so \
                        that `<img>`, `<audio>` and `<video>` render them; every other type \
                        is an `attachment`.",
         content_type = "application/octet-stream",
         body = inline(MediaFile),
         headers(
             ("Content-Length" = u64, description = "The file's size in bytes"),
             ("Content-Disposition" = String, description = "`inline; filename=\"…\"` for \
                                                             images (not SVG), audio and \
                                                             video; `attachment; filename=\"…\"` \
                                                             otherwise"),
             ("Cache-Control" = String, description = "`private, max-age=` the seconds left \
                                                       until the link expires"),
             ("X-Content-Type-Options" = String, description = "`nosniff`"),
             ("Content-Security-Policy" = String, description = "`sandbox`"),
             ("Referrer-Policy" = String, description = "`no-referrer`"),
         ),
        ),
        (status = 404, description = "The link does not open a file. One answer for every \
                                      reason (see the description)."),
        (status = 500, description = "The file exists and the link is valid, but the store \
                                      could not read it."),
    ),
)]
pub(crate) async fn download(
    State(state): State<SharedState>,
    Path(key): Path<String>,
    Query(query): Query<LinkQuery>,
) -> Response {
    let Some(store) = state.media_downloads.as_ref() else {
        return not_found();
    };
    let (Some(expires), Some(signature)) = (query.expires, query.signature) else {
        return not_found();
    };
    let now = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_secs(),
        Err(_) => return not_found(),
    };
    let download = match store.open_download(&key, &expires, &signature, now).await {
        Ok(Some(download)) => download,
        Ok(None) => return not_found(),
        Err(err) => {
            tracing::error!("media download failed: {err}");
            return (StatusCode::INTERNAL_SERVER_ERROR, "could not read the file").into_response();
        }
    };

    let content_type = HeaderValue::from_str(&download.content_type)
        .unwrap_or_else(|_| HeaderValue::from_static("application/octet-stream"));
    let disposition = if renders_inline(&download.content_type) {
        "inline"
    } else {
        "attachment"
    };
    // The file name is `file_id.ext` from a parsed key: [A-Za-z0-9_.] only.
    let disposition = HeaderValue::from_str(&format!(
        "{disposition}; filename=\"{}\"",
        download.file_name
    ))
    .unwrap_or_else(|_| HeaderValue::from_static("attachment"));
    let cache = HeaderValue::from_str(&format!("private, max-age={}", download.remaining_secs))
        .unwrap_or_else(|_| HeaderValue::from_static("no-store"));

    let body = Body::from_stream(tokio_util::io::ReaderStream::new(download.file));
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type),
            (
                header::CONTENT_LENGTH,
                HeaderValue::from(download.size_bytes),
            ),
            (header::CONTENT_DISPOSITION, disposition),
            (
                header::X_CONTENT_TYPE_OPTIONS,
                HeaderValue::from_static("nosniff"),
            ),
            (
                header::CONTENT_SECURITY_POLICY,
                HeaderValue::from_static("sandbox"),
            ),
            (header::CACHE_CONTROL, cache),
            (
                header::REFERRER_POLICY,
                HeaderValue::from_static("no-referrer"),
            ),
        ],
        body,
    )
        .into_response()
}

/// Whether a browser may show `content_type` in place: images (not SVG,
/// which runs script), audio and video. Everything else is downloaded.
fn renders_inline(content_type: &str) -> bool {
    let essence = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    (essence.starts_with("image/") && !essence.contains("svg"))
        || essence.starts_with("audio/")
        || essence.starts_with("video/")
}

fn not_found() -> Response {
    (StatusCode::NOT_FOUND, "not found").into_response()
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::Duration;

    use axum::body::to_bytes;
    use axum::http::Request;
    use bytes::Bytes;
    use tower::ServiceExt;

    use crate::services::AppState;
    use crate::services::media_storage::{
        FilesystemConfig, FilesystemStorage, LinkSigner, Storage,
    };

    const PNG: &str = "default/image/2026/11/file_01HX1234567890ABCDEF.png";
    const HTML: &str = "default/document/2026/11/file_01HX1234567890ABCDEG.html";
    const SVG: &str = "default/image/2026/11/file_01HX1234567890ABCDEH.svg";

    struct Fixture {
        app: axum::Router,
        store: Arc<FilesystemStorage>,
        dir: std::path::PathBuf,
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    async fn fixture(filesystem: bool) -> Fixture {
        let config =
            crate::config::AppConfig::for_test("postgresql://placeholder/unused".to_string());
        let dir = std::env::temp_dir().join(format!("brutor-media-route-{}", ulid::Ulid::new()));
        let store = Arc::new(
            FilesystemStorage::open(
                FilesystemConfig {
                    dir: dir.clone(),
                    max_bytes: 1_000_000,
                    public_url: config.public_url.clone(),
                },
                LinkSigner::from_jwt_secret(&config.jwt_secret),
            )
            .await
            .unwrap(),
        );
        let mut state = AppState::new(
            config,
            sqlx::postgres::PgPoolOptions::new()
                .connect_lazy("postgresql://placeholder:placeholder@127.0.0.1:1/none")
                .expect("a lazy pool"),
            None,
            reqwest::Client::new(),
            crate::services::telemetry::TelemetryState {
                prometheus_handle: None,
            },
        );
        if filesystem {
            state.media_storage = store.clone();
            state.media_downloads = Some(store.clone());
        }
        let app = crate::app::router(Arc::new(state)).expect("the application");
        Fixture { app, store, dir }
    }

    /// The path and query of a link the store mints for `key`.
    async fn link(store: &FilesystemStorage, key: &str) -> String {
        let url = store
            .presigned_get_url(key, Duration::from_secs(120))
            .await
            .unwrap();
        let parsed = url::Url::parse(&url).unwrap();
        format!("{}?{}", parsed.path(), parsed.query().unwrap())
    }

    async fn get(app: &axum::Router, uri: &str) -> Response {
        app.clone()
            .oneshot(Request::get(uri).body(Body::empty()).unwrap())
            .await
            .unwrap()
    }

    use super::*;

    #[tokio::test]
    async fn a_signed_link_serves_the_file_with_its_type_and_disposition() {
        let f = fixture(true).await;
        f.store
            .put(PNG, Bytes::from_static(b"\x89PNG-bytes"), "image/png")
            .await
            .unwrap();
        let uri = link(&f.store, PNG).await;
        assert!(uri.starts_with("/v1/media/default/image/2026/11/"), "{uri}");
        let resp = get(&f.app, &uri).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let h = resp.headers().clone();
        assert_eq!(h[header::CONTENT_TYPE], "image/png");
        assert_eq!(h[header::CONTENT_LENGTH], "10");
        assert_eq!(
            h[header::CONTENT_DISPOSITION],
            "inline; filename=\"file_01HX1234567890ABCDEF.png\""
        );
        assert_eq!(h[header::X_CONTENT_TYPE_OPTIONS], "nosniff");
        assert_eq!(h[header::CONTENT_SECURITY_POLICY], "sandbox");
        let cache = h[header::CACHE_CONTROL].to_str().unwrap();
        assert!(cache.starts_with("private, max-age="), "{cache}");
        let body = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        assert_eq!(body.as_ref(), b"\x89PNG-bytes");
    }

    /// A type a browser would run as a page of the gateway's origin is a
    /// download, never inline.
    #[tokio::test]
    async fn html_and_svg_are_attachments() {
        let f = fixture(true).await;
        f.store
            .put(HTML, Bytes::from_static(b"<script>1</script>"), "text/html")
            .await
            .unwrap();
        f.store
            .put(SVG, Bytes::from_static(b"<svg/>"), "image/svg+xml")
            .await
            .unwrap();
        for key in [HTML, SVG] {
            let resp = get(&f.app, &link(&f.store, key).await).await;
            assert_eq!(resp.status(), StatusCode::OK);
            let disposition = resp.headers()[header::CONTENT_DISPOSITION]
                .to_str()
                .unwrap();
            assert!(
                disposition.starts_with("attachment;"),
                "{key}: {disposition}"
            );
            assert_eq!(resp.headers()[header::X_CONTENT_TYPE_OPTIONS], "nosniff");
        }
    }

    /// Every link that is not valid gets the same 404: tampered, expired,
    /// unsigned, traversal, a directory, a file that is not there.
    #[tokio::test]
    async fn invalid_links_traversal_and_directories_are_404() {
        let f = fixture(true).await;
        f.store
            .put(PNG, Bytes::from_static(b"png"), "image/png")
            .await
            .unwrap();
        let good = link(&f.store, PNG).await;
        let (path, query) = good.split_once('?').unwrap();
        let expires: u64 = query
            .split('&')
            .find_map(|p| p.strip_prefix("expires="))
            .unwrap()
            .parse()
            .unwrap();
        let signature = query
            .split('&')
            .find_map(|p| p.strip_prefix("signature="))
            .unwrap();
        let other = "default/image/2026/11/file_01HX1234567890ABCDEZ.png";
        f.store
            .put(other, Bytes::from_static(b"other"), "image/png")
            .await
            .unwrap();
        let mut flipped = signature.to_string().into_bytes();
        flipped[5] = if flipped[5] == b'a' { b'b' } else { b'a' };
        let flipped = String::from_utf8(flipped).unwrap();
        for uri in [
            path.to_string(),
            format!("{path}?expires={expires}"),
            format!("{path}?signature={signature}"),
            format!("{path}?expires={}&signature={signature}", expires + 60),
            format!("{path}?expires=1&signature={signature}"),
            format!("{path}?expires={expires}&signature={flipped}"),
            format!("/v1/media/{other}?{query}"),
            format!("/v1/media/default/image/2026/11/../../../../../etc/passwd?{query}"),
            format!("/v1/media/default/image/2026/11/..%2f..%2f..%2fetc%2fpasswd?{query}"),
            format!("/v1/media/%2e%2e/%2e%2e/etc/passwd?{query}"),
            format!("/v1/media/default/?{query}"),
            format!("/v1/media/default/image/2026/11?{query}"),
            format!("/v1/media/default/image/2026/11/?{query}"),
            format!("/v1/media/.tmp/x?{query}"),
            format!("/v1/media/{PNG}.meta?{query}"),
            "/v1/media/".to_string(),
        ] {
            let resp = get(&f.app, &uri).await;
            assert_eq!(resp.status(), StatusCode::NOT_FOUND, "{uri}");
            let body = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
            assert!(!body.windows(3).any(|w| w == b"png"), "{uri}: {body:?}");
        }
        // A valid link to a file that is gone.
        f.store.delete(PNG).await.unwrap();
        assert_eq!(get(&f.app, &good).await.status(), StatusCode::NOT_FOUND);
    }

    /// A gateway whose store is not the filesystem one serves no link.
    #[tokio::test]
    async fn without_the_filesystem_store_every_link_is_404() {
        let f = fixture(false).await;
        f.store
            .put(PNG, Bytes::from_static(b"png"), "image/png")
            .await
            .unwrap();
        let uri = link(&f.store, PNG).await;
        assert_eq!(get(&f.app, &uri).await.status(), StatusCode::NOT_FOUND);
    }
}
