// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Storage trait + in-memory test double.
//!
//! Higher layers (route handlers, the GC sweep, the upload pipeline)
//! depend on the [`Storage`] trait, never on a concrete backend.
//! That keeps the unit-test path tight: in-process tests use
//! [`InMemoryStorage`], integration tests against MinIO use the
//! `S3Storage` impl in [`super::s3`].

use async_trait::async_trait;
use bytes::Bytes;
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Object metadata returned by [`Storage::head`]. Mirrors the
/// fields that consumers actually use — anything backend-specific
/// (S3 ETag, version id) is intentionally not surfaced so the
/// abstraction stays tight.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObjectMetadata {
    /// Byte length of the stored object.
    pub size_bytes: u64,
    /// IANA media type (e.g. `image/png`). Echoed back from the
    /// `Content-Type` recorded at write time.
    pub content_type: String,
    /// Last-modified Unix-seconds timestamp.
    pub last_modified_unix_secs: u64,
}

/// Errors from the [`Storage`] trait.
///
/// Backend-specific error categories collapse into these so
/// callers don't need to special-case S3 vs MinIO. The string
/// payload is for logging — never expose it to clients verbatim
/// (it can leak bucket / endpoint detail).
#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    /// The key resolved to no object. 404-equivalent.
    #[error("not found: {key}")]
    NotFound { key: String },
    /// The backend rejected the request because of permissions.
    /// Should never happen with a correctly-configured tenant
    /// prefix; treat as an alert-worthy 500.
    #[error("permission denied: {0}")]
    PermissionDenied(String),
    /// Network / 5xx / SDK error. Caller may retry.
    #[error("backend error: {0}")]
    Backend(String),
    /// Caller-supplied input was malformed (e.g. empty body, key
    /// past max length). 400-equivalent.
    #[error("invalid input: {0}")]
    Invalid(String),
    /// The store has no room for the write: the filesystem store's size
    /// limit. 507-equivalent; the message names the setting.
    #[error("media store full: {0}")]
    Full(String),
}

/// The unified API gateway-core uses to talk to object storage.
/// Implementations must be `Send + Sync` so they can sit on
/// `Arc<dyn Storage>` in the shared `AppState`.
#[async_trait]
pub trait Storage: Send + Sync {
    /// Write `bytes` to `key` with the given `content_type`. If
    /// the key already exists, the bytes are overwritten — the
    /// caller is responsible for content-hash dedup (we use the
    /// hash as part of the file_id so this is rarely hit in
    /// practice, but the contract allows it).
    async fn put(&self, key: &str, bytes: Bytes, content_type: &str) -> Result<(), StorageError>;

    /// Fetch the bytes at `key`. Returns [`StorageError::NotFound`]
    /// if absent.
    async fn get(&self, key: &str) -> Result<Bytes, StorageError>;

    /// Return metadata without reading the bytes. Cheap probe
    /// used by the GC sweep before a hard-delete to confirm the
    /// object exists (so a missing object doesn't block the row
    /// removal).
    async fn head(&self, key: &str) -> Result<ObjectMetadata, StorageError>;

    /// Delete the object at `key`. Idempotent — deleting a missing
    /// object returns Ok(()) (the GC sweep relies on this so a
    /// half-completed previous run doesn't keep crashing).
    async fn delete(&self, key: &str) -> Result<(), StorageError>;

    /// Mint a presigned download URL with the given TTL. The URL
    /// is single-use-or-not depending on the backend; we treat it
    /// as time-bounded only — clients re-fetch through
    /// `GET /v1/proxy/files/{id}/url` instead of caching.
    async fn presigned_get_url(&self, key: &str, ttl: Duration) -> Result<String, StorageError>;
}

// ---------------------------------------------------------------------------
// InMemoryStorage — test double
// ---------------------------------------------------------------------------

/// An in-process bytes store. Lives in the production crate
/// (rather than `cfg(test)`) because the route-handler tests in
/// `routes::files` and the GC-sweep tests in
/// `services::media_storage::sweep` need to
/// instantiate it. Not safe for production use — it has no quota
/// enforcement of its own and stores bytes in RAM.
#[derive(Default)]
pub struct InMemoryStorage {
    inner: Mutex<HashMap<String, StoredObject>>,
}

#[derive(Clone)]
struct StoredObject {
    bytes: Bytes,
    content_type: String,
    last_modified_unix_secs: u64,
}

impl InMemoryStorage {
    pub fn new() -> Self {
        Self::default()
    }

    /// How many objects are currently held. Test helper.
    pub fn len(&self) -> usize {
        self.inner.lock().expect("InMemoryStorage poisoned").len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[async_trait]
impl Storage for InMemoryStorage {
    async fn put(&self, key: &str, bytes: Bytes, content_type: &str) -> Result<(), StorageError> {
        if key.is_empty() {
            return Err(StorageError::Invalid("empty key".into()));
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let mut guard = self.inner.lock().expect("InMemoryStorage poisoned");
        guard.insert(
            key.to_string(),
            StoredObject {
                bytes,
                content_type: content_type.to_string(),
                last_modified_unix_secs: now,
            },
        );
        Ok(())
    }

    async fn get(&self, key: &str) -> Result<Bytes, StorageError> {
        let guard = self.inner.lock().expect("InMemoryStorage poisoned");
        guard
            .get(key)
            .map(|o| o.bytes.clone())
            .ok_or_else(|| StorageError::NotFound { key: key.into() })
    }

    async fn head(&self, key: &str) -> Result<ObjectMetadata, StorageError> {
        let guard = self.inner.lock().expect("InMemoryStorage poisoned");
        guard
            .get(key)
            .map(|o| ObjectMetadata {
                size_bytes: o.bytes.len() as u64,
                content_type: o.content_type.clone(),
                last_modified_unix_secs: o.last_modified_unix_secs,
            })
            .ok_or_else(|| StorageError::NotFound { key: key.into() })
    }

    async fn delete(&self, key: &str) -> Result<(), StorageError> {
        let mut guard = self.inner.lock().expect("InMemoryStorage poisoned");
        guard.remove(key);
        // Idempotent — missing key is not an error.
        Ok(())
    }

    async fn presigned_get_url(&self, key: &str, _ttl: Duration) -> Result<String, StorageError> {
        // The in-memory backend doesn't have a real HTTP surface,
        // so we return a synthetic URL the test layer can pattern-
        // match on. Production paths hit the S3 impl, never this.
        let guard = self.inner.lock().expect("InMemoryStorage poisoned");
        if !guard.contains_key(key) {
            return Err(StorageError::NotFound { key: key.into() });
        }
        Ok(format!("memory://{key}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn put_then_get_round_trips() {
        let s = InMemoryStorage::new();
        s.put("k1", Bytes::from_static(b"hello"), "text/plain")
            .await
            .unwrap();
        let got = s.get("k1").await.unwrap();
        assert_eq!(got.as_ref(), b"hello");
    }

    #[tokio::test]
    async fn get_missing_is_not_found() {
        let s = InMemoryStorage::new();
        let err = s.get("missing").await.unwrap_err();
        assert!(matches!(err, StorageError::NotFound { .. }));
    }

    #[tokio::test]
    async fn head_returns_size_and_content_type() {
        let s = InMemoryStorage::new();
        s.put("k", Bytes::from_static(b"abcde"), "image/png")
            .await
            .unwrap();
        let meta = s.head("k").await.unwrap();
        assert_eq!(meta.size_bytes, 5);
        assert_eq!(meta.content_type, "image/png");
    }

    #[tokio::test]
    async fn delete_is_idempotent() {
        let s = InMemoryStorage::new();
        s.put("k", Bytes::from_static(b"x"), "text/plain")
            .await
            .unwrap();
        s.delete("k").await.unwrap();
        // Second delete: must NOT error — GC sweep relies on this.
        s.delete("k").await.unwrap();
        // Confirm gone
        assert!(matches!(
            s.get("k").await,
            Err(StorageError::NotFound { .. })
        ));
    }

    #[tokio::test]
    async fn put_overwrite_replaces_bytes_and_content_type() {
        let s = InMemoryStorage::new();
        s.put("k", Bytes::from_static(b"v1"), "text/plain")
            .await
            .unwrap();
        s.put("k", Bytes::from_static(b"v2-longer"), "image/png")
            .await
            .unwrap();
        let meta = s.head("k").await.unwrap();
        assert_eq!(meta.size_bytes, 9);
        assert_eq!(meta.content_type, "image/png");
        assert_eq!(s.get("k").await.unwrap().as_ref(), b"v2-longer");
    }

    #[tokio::test]
    async fn empty_key_rejected() {
        let s = InMemoryStorage::new();
        let err = s
            .put("", Bytes::from_static(b"x"), "text/plain")
            .await
            .unwrap_err();
        assert!(matches!(err, StorageError::Invalid(_)));
    }

    #[tokio::test]
    async fn presigned_url_returns_synthetic_for_existing_key() {
        let s = InMemoryStorage::new();
        s.put(
            "acme/image/2026/04/file_x.png",
            Bytes::from_static(b"x"),
            "image/png",
        )
        .await
        .unwrap();
        let url = s
            .presigned_get_url("acme/image/2026/04/file_x.png", Duration::from_secs(60))
            .await
            .unwrap();
        assert_eq!(url, "memory://acme/image/2026/04/file_x.png");
    }

    #[tokio::test]
    async fn presigned_url_for_missing_key_is_not_found() {
        let s = InMemoryStorage::new();
        let err = s
            .presigned_get_url("missing", Duration::from_secs(60))
            .await
            .unwrap_err();
        assert!(matches!(err, StorageError::NotFound { .. }));
    }

    #[tokio::test]
    async fn len_reflects_writes_and_deletes() {
        let s = InMemoryStorage::new();
        assert!(s.is_empty());
        s.put("a", Bytes::from_static(b"1"), "x").await.unwrap();
        s.put("b", Bytes::from_static(b"2"), "x").await.unwrap();
        assert_eq!(s.len(), 2);
        s.delete("a").await.unwrap();
        assert_eq!(s.len(), 1);
    }
}
