// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Production [`Storage`] implementation backed by S3 / MinIO.
//!
//! Both AWS S3 (SaaS) and MinIO (trial bundle) speak the same
//! protocol, so a single `S3Storage` struct drives both. The
//! difference is the [`S3Config::endpoint_url`]:
//!
//! - **AWS S3**: leave as `None`. The SDK resolves the endpoint
//!   from the region using AWS's standard service discovery.
//! - **MinIO** (or any other S3-compatible store): set to the
//!   MinIO host, e.g. `http://minio:9000`. We also force
//!   path-style addressing because MinIO doesn't do
//!   virtual-hosted-style routing reliably for ad-hoc bucket
//!   names.
//!
//! Credentials follow the standard AWS chain on AWS, and come
//! from explicit env vars (`BRUTOR_MEDIA_S3_ACCESS_KEY_ID` /
//! `BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY`) on MinIO.
//!
//! ## Internal vs public endpoint
//!
//! Trial-bundle deployments need a **split** endpoint:
//!
//! - The proxy talks to MinIO over the docker network at
//!   `http://minio:9000` (DNS only resolvable inside the compose
//!   project) — `endpoint_url`.
//! - Browsers receive presigned download URLs and must reach
//!   MinIO from the host. The hostname has to be one the browser
//!   can resolve and that the portal's CSP allows — typically
//!   `http://localhost:9000` — `signing_endpoint_url`.
//!
//! When `signing_endpoint_url` is `Some`, [`S3Storage`] keeps a
//! second AWS SDK client wired to that endpoint and routes
//! `presigned_get_url` calls through it. All real I/O still uses
//! the internal client. The signing client is never used for
//! actual transport — only as a URL builder — so it does not
//! need to be reachable from the proxy at runtime.
//!
//! On AWS S3 (SaaS) leave both unset: the SDK resolves the public
//! S3 endpoint by region and the same client both uploads and
//! signs.

use async_trait::async_trait;
use aws_config::BehaviorVersion;
use aws_credential_types::Credentials;
use aws_sdk_s3::Client;
use aws_sdk_s3::config::{Region, SharedCredentialsProvider};
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::primitives::ByteStream;
use bytes::Bytes;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use super::storage::{ObjectMetadata, Storage, StorageError};

/// Configuration for [`S3Storage`].
///
/// Built from `AppConfig` env vars by [`S3Config::from_env`].
/// The constructor is also `pub` so tests can hand-build a config
/// pointing at a local MinIO without touching the process env.
#[derive(Clone, Debug)]
pub struct S3Config {
    /// S3 region. AWS S3: real region (e.g. `us-east-1`). MinIO:
    /// any string — the SDK requires one but MinIO ignores it.
    pub region: String,
    /// Bucket name. The same bucket holds every tenant's media;
    /// per-tenant isolation is enforced by the leading
    /// tenant-prefix in the object key + bucket policy SCP.
    pub bucket: String,
    /// Custom endpoint. `None` for AWS S3, `Some("http://...:9000")`
    /// for MinIO.
    pub endpoint_url: Option<String>,
    /// Public endpoint used **only** to mint presigned URLs.
    /// `None` collapses to single-endpoint mode (signed URLs use
    /// `endpoint_url` / the SDK default). Set this for trial-
    /// bundle deployments where the proxy talks to MinIO over the
    /// docker network (`http://minio:9000`) but browsers need a
    /// host they can reach (`http://localhost:9000`).
    pub signing_endpoint_url: Option<String>,
    /// Force path-style addressing. Required for MinIO;
    /// recommended off for AWS S3 (virtual-hosted is the modern
    /// addressing style).
    pub force_path_style: bool,
    /// Static credentials for self-hosted / MinIO. On AWS, leave
    /// `None` and the standard credential chain applies (env →
    /// shared config → IMDS → ECS task role).
    pub static_credentials: Option<S3Credentials>,
}

#[derive(Clone, Debug)]
pub struct S3Credentials {
    pub access_key_id: String,
    pub secret_access_key: String,
    pub session_token: Option<String>,
}

/// Production S3-backed [`Storage`].
///
/// Cheap to clone — internally holds an `aws_sdk_s3::Client` which
/// itself wraps an `Arc` over the connection pool. When the
/// caller configured a `signing_endpoint_url`, a second client is
/// kept solely to mint presigned URLs against the public host.
#[derive(Clone)]
pub struct S3Storage {
    client: Client,
    /// Optional separate client used only by `presigned_get_url`.
    /// `None` means "single endpoint" — sign with `client`.
    signing_client: Option<Client>,
    bucket: String,
}

impl S3Storage {
    /// Build the S3 client from `cfg` and verify the bucket is
    /// reachable. We do the head-bucket probe at construction
    /// time so a misconfiguration fails the boot loud and early —
    /// not at the first user upload.
    pub async fn new(cfg: S3Config) -> Result<Self, StorageError> {
        let client = build_client(&cfg, cfg.endpoint_url.clone()).await;

        // Probe — head_bucket is cheap, returns quickly, and
        // catches every common misconfig (wrong endpoint, wrong
        // creds, bucket missing) with a clear error.
        client
            .head_bucket()
            .bucket(&cfg.bucket)
            .send()
            .await
            .map_err(|err| {
                StorageError::Backend(format!("head_bucket(bucket={}) failed: {err}", cfg.bucket))
            })?;

        // Build the signing-only client when a public endpoint is
        // configured. We do NOT probe this one — it points at a
        // host the proxy may not be able to reach (e.g. the
        // browser-facing `http://localhost:9000` from inside the
        // docker network), but presigning is purely local URL
        // construction so reachability is irrelevant.
        let signing_client = match &cfg.signing_endpoint_url {
            Some(ep) if Some(ep) != cfg.endpoint_url.as_ref() => {
                Some(build_client(&cfg, Some(ep.clone())).await)
            }
            _ => None,
        };

        Ok(Self {
            client,
            signing_client,
            bucket: cfg.bucket,
        })
    }

    /// Bucket-name accessor — used by the GC sweep to log which
    /// bucket it just hard-deleted from.
    pub fn bucket(&self) -> &str {
        &self.bucket
    }
}

async fn build_client(cfg: &S3Config, endpoint_url: Option<String>) -> Client {
    let mut loader =
        aws_config::defaults(BehaviorVersion::latest()).region(Region::new(cfg.region.clone()));

    if let Some(creds) = &cfg.static_credentials {
        let credentials = Credentials::new(
            creds.access_key_id.clone(),
            creds.secret_access_key.clone(),
            creds.session_token.clone(),
            None,
            "brutor-static",
        );
        loader = loader.credentials_provider(SharedCredentialsProvider::new(credentials));
    }

    let shared = loader.load().await;
    let mut s3_cfg = aws_sdk_s3::config::Builder::from(&shared);
    if let Some(ep) = endpoint_url {
        s3_cfg = s3_cfg.endpoint_url(ep);
    }
    s3_cfg = s3_cfg.force_path_style(cfg.force_path_style);

    Client::from_conf(s3_cfg.build())
}

#[async_trait]
impl Storage for S3Storage {
    async fn put(&self, key: &str, bytes: Bytes, content_type: &str) -> Result<(), StorageError> {
        if key.is_empty() {
            return Err(StorageError::Invalid("empty key".into()));
        }
        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(key)
            .body(ByteStream::from(bytes))
            .content_type(content_type)
            .send()
            .await
            .map_err(|err| StorageError::Backend(format!("put_object key={key}: {err}")))?;
        Ok(())
    }

    async fn get(&self, key: &str) -> Result<Bytes, StorageError> {
        let resp = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
            .map_err(|err| classify_get_error(key, err))?;

        let body = resp
            .body
            .collect()
            .await
            .map_err(|err| StorageError::Backend(format!("read body key={key}: {err}")))?;
        Ok(body.into_bytes())
    }

    async fn head(&self, key: &str) -> Result<ObjectMetadata, StorageError> {
        let resp = self
            .client
            .head_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
            .map_err(|err| classify_head_error(key, err))?;

        let size = resp.content_length().unwrap_or(0).max(0) as u64;
        let content_type = resp
            .content_type()
            .unwrap_or("application/octet-stream")
            .to_string();
        let last_modified = resp
            .last_modified()
            .and_then(|dt| {
                let secs = dt.secs();
                if secs >= 0 { Some(secs as u64) } else { None }
            })
            .unwrap_or_else(|| {
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0)
            });

        Ok(ObjectMetadata {
            size_bytes: size,
            content_type,
            last_modified_unix_secs: last_modified,
        })
    }

    async fn delete(&self, key: &str) -> Result<(), StorageError> {
        // S3's DeleteObject is already idempotent — deleting a
        // missing key returns 204. We forward the call as-is.
        self.client
            .delete_object()
            .bucket(&self.bucket)
            .key(key)
            .send()
            .await
            .map_err(|err| StorageError::Backend(format!("delete_object key={key}: {err}")))?;
        Ok(())
    }

    async fn presigned_get_url(&self, key: &str, ttl: Duration) -> Result<String, StorageError> {
        let presign = PresigningConfig::builder()
            .expires_in(ttl)
            .build()
            .map_err(|err| StorageError::Invalid(format!("presign config: {err}")))?;

        // Sign against the public client when one was configured.
        // SigV4 binds the signature to the host header, so a URL
        // signed by the internal client (`http://minio:9000`)
        // cannot be served to a browser hitting a different host
        // — even when both reach the same MinIO. The signing
        // client is set up in `S3Storage::new` to use the public
        // endpoint when it differs from the upload endpoint.
        let signer = self.signing_client.as_ref().unwrap_or(&self.client);
        let presigned = signer
            .get_object()
            .bucket(&self.bucket)
            .key(key)
            .presigned(presign)
            .await
            .map_err(|err| StorageError::Backend(format!("presign key={key}: {err}")))?;
        Ok(presigned.uri().to_string())
    }
}

// ---------------------------------------------------------------------------
// Error classification helpers
// ---------------------------------------------------------------------------

fn classify_get_error<E: std::fmt::Display>(key: &str, err: E) -> StorageError {
    let msg = err.to_string();
    if is_no_such_key(&msg) {
        StorageError::NotFound { key: key.into() }
    } else if is_access_denied(&msg) {
        StorageError::PermissionDenied(msg)
    } else {
        StorageError::Backend(format!("get_object key={key}: {msg}"))
    }
}

fn classify_head_error<E: std::fmt::Display>(key: &str, err: E) -> StorageError {
    let msg = err.to_string();
    // head_object returns "NotFound" rather than "NoSuchKey" — the
    // S3 head API doesn't include the body code, so the SDK
    // surfaces it as a different variant.
    if msg.contains("NotFound") || msg.contains("NoSuchKey") || msg.contains("404") {
        StorageError::NotFound { key: key.into() }
    } else if is_access_denied(&msg) {
        StorageError::PermissionDenied(msg)
    } else {
        StorageError::Backend(format!("head_object key={key}: {msg}"))
    }
}

fn is_no_such_key(msg: &str) -> bool {
    msg.contains("NoSuchKey") || msg.contains("404")
}

fn is_access_denied(msg: &str) -> bool {
    msg.contains("AccessDenied") || msg.contains("403")
}

#[cfg(test)]
mod tests {
    //! Integration tests for [`S3Storage`].
    //!
    //! These tests are skipped unless `BRUTOR_S3_TEST_ENDPOINT` is
    //! set, because they need a live MinIO (or AWS S3) endpoint.
    //! Local dev workflow: bring up MinIO from
    //! `docker-compose.yml` and export the env vars before
    //! running `cargo test --lib services::media_storage::s3`.
    //!
    //! Required env vars (skip path runs when any is missing):
    //! - `BRUTOR_S3_TEST_ENDPOINT`  — e.g. `http://localhost:9000`
    //! - `BRUTOR_S3_TEST_BUCKET`    — pre-created bucket
    //! - `BRUTOR_S3_TEST_ACCESS_KEY`
    //! - `BRUTOR_S3_TEST_SECRET_KEY`

    use super::*;

    fn config_from_env() -> Option<S3Config> {
        let endpoint = std::env::var("BRUTOR_S3_TEST_ENDPOINT").ok()?;
        let bucket = std::env::var("BRUTOR_S3_TEST_BUCKET").ok()?;
        let access_key = std::env::var("BRUTOR_S3_TEST_ACCESS_KEY").ok()?;
        let secret_key = std::env::var("BRUTOR_S3_TEST_SECRET_KEY").ok()?;
        Some(S3Config {
            region: "us-east-1".into(),
            bucket,
            endpoint_url: Some(endpoint),
            // Tests run against a single endpoint. The split-
            // endpoint behaviour is exercised by
            // `presigned_url_uses_signing_endpoint_when_split` in
            // the unit-test module below — it doesn't need a
            // running MinIO.
            signing_endpoint_url: None,
            force_path_style: true,
            static_credentials: Some(S3Credentials {
                access_key_id: access_key,
                secret_access_key: secret_key,
                session_token: None,
            }),
        })
    }

    macro_rules! skip_if_no_endpoint {
        () => {
            match config_from_env() {
                Some(cfg) => cfg,
                None => {
                    eprintln!(
                        "[skip] BRUTOR_S3_TEST_ENDPOINT not set; \
                         skipping S3Storage integration test."
                    );
                    return;
                }
            }
        };
    }

    #[tokio::test]
    async fn round_trip_put_get_delete() {
        let cfg = skip_if_no_endpoint!();
        let storage = S3Storage::new(cfg).await.expect("S3Storage::new");
        let key = format!("brutor-it/{}.bin", ulid::Ulid::new());
        let body = Bytes::from_static(b"hello, integration");

        storage
            .put(&key, body.clone(), "application/octet-stream")
            .await
            .expect("put");

        let got = storage.get(&key).await.expect("get");
        assert_eq!(got.as_ref(), b"hello, integration");

        let head = storage.head(&key).await.expect("head");
        assert_eq!(head.size_bytes, body.len() as u64);
        assert_eq!(head.content_type, "application/octet-stream");

        storage.delete(&key).await.expect("delete");
        // S3 deletes are eventual on some backends, but on MinIO
        // and modern S3 it's strong-read-after-delete consistent.
        assert!(matches!(
            storage.get(&key).await,
            Err(StorageError::NotFound { .. })
        ));
    }

    #[tokio::test]
    async fn delete_missing_key_is_idempotent() {
        let cfg = skip_if_no_endpoint!();
        let storage = S3Storage::new(cfg).await.expect("S3Storage::new");
        // Random key that doesn't exist — delete must succeed.
        let key = format!("brutor-it/missing/{}.bin", ulid::Ulid::new());
        storage.delete(&key).await.expect("delete missing key");
    }

    #[tokio::test]
    async fn presigned_url_is_minted_for_existing_key() {
        let cfg = skip_if_no_endpoint!();
        let storage = S3Storage::new(cfg).await.expect("S3Storage::new");
        let key = format!("brutor-it/{}.txt", ulid::Ulid::new());
        storage
            .put(&key, Bytes::from_static(b"sign me"), "text/plain")
            .await
            .expect("put");

        let url = storage
            .presigned_get_url(&key, Duration::from_secs(60))
            .await
            .expect("presign");
        assert!(
            url.contains(&key),
            "presigned URL should contain the key, got {url}"
        );
        assert!(
            url.starts_with("http"),
            "presigned URL should start with http(s), got {url}"
        );

        storage.delete(&key).await.ok();
    }

    #[tokio::test]
    async fn head_missing_key_is_not_found() {
        let cfg = skip_if_no_endpoint!();
        let storage = S3Storage::new(cfg).await.expect("S3Storage::new");
        let key = format!("brutor-it/missing/{}.txt", ulid::Ulid::new());
        let err = storage.head(&key).await.unwrap_err();
        assert!(
            matches!(err, StorageError::NotFound { .. }),
            "expected NotFound, got {err:?}"
        );
    }
}

// ---------------------------------------------------------------------------
// Unit tests — split-endpoint signing
// ---------------------------------------------------------------------------
//
// These don't need a real S3/MinIO. The AWS SDK's `presigned()`
// is pure URL construction: signs the canonical request with the
// configured credentials + endpoint and returns a URL string.
// Reachability isn't exercised, which is the whole point — the
// signing client points at a host the proxy cannot reach.

#[cfg(test)]
mod unit_tests {
    use super::*;

    fn cfg(endpoint: &str, signing_endpoint: Option<&str>) -> S3Config {
        S3Config {
            region: "us-east-1".into(),
            bucket: "brutor-media".into(),
            endpoint_url: Some(endpoint.into()),
            signing_endpoint_url: signing_endpoint.map(str::to_string),
            force_path_style: true,
            static_credentials: Some(S3Credentials {
                access_key_id: "test-key".into(),
                secret_access_key: "test-secret".into(),
                session_token: None,
            }),
        }
    }

    /// Manually construct an `S3Storage` skipping the
    /// `head_bucket` probe — `S3Storage::new` is async-and-
    /// reachability-coupled so we can't use it for pure URL-
    /// construction tests.
    async fn storage_for(cfg: &S3Config) -> S3Storage {
        let client = build_client(cfg, cfg.endpoint_url.clone()).await;
        let signing_client = match &cfg.signing_endpoint_url {
            Some(ep) if Some(ep) != cfg.endpoint_url.as_ref() => {
                Some(build_client(cfg, Some(ep.clone())).await)
            }
            _ => None,
        };
        S3Storage {
            client,
            signing_client,
            bucket: cfg.bucket.clone(),
        }
    }

    #[tokio::test]
    async fn presigned_url_uses_internal_endpoint_when_no_split() {
        let storage = storage_for(&cfg("http://minio:9000", None)).await;
        let url = storage
            .presigned_get_url("default/img.png", Duration::from_secs(60))
            .await
            .expect("presign");
        assert!(
            url.starts_with("http://minio:9000/"),
            "expected internal host, got {url}"
        );
        assert!(
            url.contains("default/img.png"),
            "URL should contain the object key, got {url}"
        );
    }

    #[tokio::test]
    async fn presigned_url_uses_signing_endpoint_when_split() {
        // Trial-bundle layout: proxy uploads via http://minio:9000
        // (docker network) but browsers fetch via http://localhost:9000
        // (host port). The signed URL must carry the public host
        // because SigV4 binds the signature to the Host header.
        let storage = storage_for(&cfg("http://minio:9000", Some("http://localhost:9000"))).await;
        let url = storage
            .presigned_get_url("default/img.png", Duration::from_secs(60))
            .await
            .expect("presign");
        assert!(
            url.starts_with("http://localhost:9000/"),
            "split endpoint must produce a public-host URL, got {url}"
        );
        assert!(
            !url.contains("minio:9000"),
            "public URL must NOT leak the internal hostname, got {url}"
        );
    }

    #[tokio::test]
    async fn presigned_url_skips_split_when_endpoints_match() {
        // Defensive: configuring both env vars to the same value
        // shouldn't spawn a redundant client. Falls through to
        // single-endpoint signing.
        let storage =
            storage_for(&cfg("http://localhost:9000", Some("http://localhost:9000"))).await;
        assert!(
            storage.signing_client.is_none(),
            "matching endpoints should collapse to single-client mode"
        );
        let url = storage
            .presigned_get_url("k.png", Duration::from_secs(60))
            .await
            .expect("presign");
        assert!(url.starts_with("http://localhost:9000/"));
    }

    #[tokio::test]
    async fn aws_s3_no_endpoint_keeps_signing_via_default_resolver() {
        // SaaS path: neither endpoint set. Both clients fall
        // back to the SDK's region-based public S3 host, so the
        // signing-only client is unnecessary.
        let cfg = S3Config {
            region: "us-east-1".into(),
            bucket: "brutor-media".into(),
            endpoint_url: None,
            signing_endpoint_url: None,
            force_path_style: false,
            static_credentials: Some(S3Credentials {
                access_key_id: "test-key".into(),
                secret_access_key: "test-secret".into(),
                session_token: None,
            }),
        };
        let storage = storage_for(&cfg).await;
        assert!(storage.signing_client.is_none());
    }
}
