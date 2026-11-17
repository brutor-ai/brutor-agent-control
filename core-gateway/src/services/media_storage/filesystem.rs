// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! `FilesystemStorage`: media kept as files under one directory (a volume
//! mounted into the gateway), for an installation of ONE gateway instance.
//! The gateway serves the signed download links itself
//! (`routes::media`, `GET /v1/media/{key}?expires=…&signature=…`).
//!
//! ## Layout
//!
//! ```text
//! <BRUTOR_MEDIA_FILESYSTEM_DIR>/
//!   .tmp/                                        writes in progress
//!   <tenant>/<kind>/<yyyy>/<mm>/<file_id>.<ext>        the bytes
//!   <tenant>/<kind>/<yyyy>/<mm>/<file_id>.<ext>.meta   its Content-Type
//! ```
//!
//! The same object key the S3 store uses names the file, so a tenant's files
//! are under the tenant's own directory as they are under the tenant's own
//! prefix in a bucket. A key reaches the disk only through
//! [`ObjectKey::parse`], which accepts nothing but the layout
//! `ObjectKey::build` writes (every segment from a closed alphabet without
//! `.` or a separator, the file name `file_id.ext`): no key a caller can
//! send names a path outside the directory. Names are ids the gateway
//! minted, never a caller's file name.
//!
//! ## Writes
//!
//! Atomic: the bytes are written to a temporary file under `.tmp/` (the
//! same file system), flushed to disk, and renamed into place. A reader
//! sees the old file or the new one, never a part. Temporary files left by
//! a crash are removed at the next start.
//!
//! ## The size limit
//!
//! `BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES` bounds the bytes of the stored files.
//! The total is counted from the directory at start and kept in the
//! process; a write is reserved against it before a byte is written, and a
//! write that would pass the limit is refused ([`StorageError::Full`]) with
//! a message that names the setting. A delete (the media GC) gives the
//! bytes back. One process owns the directory (one gateway instance), so
//! the reservation is atomic where it matters. The tenants' own media
//! quotas (`tenants.media_storage_quota_bytes`) are checked before the
//! store is asked, as with any store.
//!
//! ## Signed download links
//!
//! A link names the key and an expiry and carries
//! `HMAC-SHA256(k, "brutor-media-link/v1\n" + tenant + "\n" + key + "\n" + expires)`,
//! hex. `k` is derived from `JWT_SECRET`, which every gateway already has:
//! `k = HMAC-SHA256(JWT_SECRET, "brutor/media-download-link/v1")`, so no
//! new secret is needed and a link is useless to anyone without the
//! gateway's secret. The tag is compared in constant time
//! (`Mac::verify_slice`). Unknown, expired, tampered and malformed links all
//! answer 404, so a guess learns nothing. Rotating `JWT_SECRET` invalidates
//! the links handed out (they live `BRUTOR_MEDIA_SIGNED_URL_TTL_SECS`, 15
//! minutes by default); a client asks `GET /v1/proxy/files/{id}/url` again.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use async_trait::async_trait;
use bytes::Bytes;
use hmac::{Hmac, Mac};
use sha2::Sha256;
use tokio::io::AsyncWriteExt;

use super::path::ObjectKey;
use super::storage::{ObjectMetadata, Storage, StorageError};

/// The directory of writes in progress, under the store's root.
pub const TMP_DIR: &str = ".tmp";
/// The suffix of the file that records an object's Content-Type.
pub const META_SUFFIX: &str = ".meta";
/// Where the gateway serves the links of this store.
pub const DOWNLOAD_PREFIX: &str = "/v1/media/";

type HmacSha256 = Hmac<Sha256>;

/// The label the link key is derived under: a key used for nothing else.
const KEY_LABEL: &[u8] = b"brutor/media-download-link/v1";
/// The first line of every signed message.
const MESSAGE_LABEL: &str = "brutor-media-link/v1";

/// What a filesystem store is opened with.
#[derive(Clone, Debug)]
pub struct FilesystemConfig {
    /// `BRUTOR_MEDIA_FILESYSTEM_DIR`.
    pub dir: PathBuf,
    /// `BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES`.
    pub max_bytes: u64,
    /// `BRUTOR_GATEWAY_PUBLIC_URL`: the base of the links.
    pub public_url: String,
}

/// Signs and checks download links. The key is derived from `JWT_SECRET`
/// (module documentation).
#[derive(Clone)]
pub struct LinkSigner {
    key: [u8; 32],
}

impl std::fmt::Debug for LinkSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LinkSigner(..)")
    }
}

impl LinkSigner {
    /// The signer of a gateway whose `JWT_SECRET` is `jwt_secret`.
    pub fn from_jwt_secret(jwt_secret: &str) -> Self {
        let mut mac = HmacSha256::new_from_slice(jwt_secret.as_bytes())
            .expect("HMAC takes a key of any length");
        mac.update(KEY_LABEL);
        Self {
            key: mac.finalize().into_bytes().into(),
        }
    }

    fn mac(&self, key: &ObjectKey, expires: u64) -> HmacSha256 {
        let mut mac =
            HmacSha256::new_from_slice(&self.key).expect("HMAC takes a key of any length");
        mac.update(
            format!(
                "{MESSAGE_LABEL}\n{}\n{}\n{expires}",
                key.tenant_prefix(),
                key.as_str()
            )
            .as_bytes(),
        );
        mac
    }

    /// The signature of a link to `key` that expires at `expires` (Unix
    /// seconds), hex.
    pub fn sign(&self, key: &ObjectKey, expires: u64) -> String {
        hex::encode(self.mac(key, expires).finalize().into_bytes())
    }

    /// Whether `signature` is this signer's for `key` and `expires`, and
    /// `expires` is not before `now` (Unix seconds).
    pub fn verify(&self, key: &ObjectKey, expires: u64, signature: &str, now: u64) -> bool {
        if expires < now {
            return false;
        }
        let Ok(tag) = hex::decode(signature) else {
            return false;
        };
        // Constant time: `verify_slice` compares the whole tag.
        self.mac(key, expires).verify_slice(&tag).is_ok()
    }
}

/// An open file a valid link names.
#[derive(Debug)]
pub struct Download {
    pub file: tokio::fs::File,
    pub size_bytes: u64,
    pub content_type: String,
    /// `file_id.ext`.
    pub file_name: String,
    /// Seconds the link has left.
    pub remaining_secs: u64,
}

/// The media store on a directory.
pub struct FilesystemStorage {
    root: PathBuf,
    max_bytes: u64,
    used: Mutex<u64>,
    signer: LinkSigner,
    public_url: String,
}

impl std::fmt::Debug for FilesystemStorage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FilesystemStorage")
            .field("root", &self.root)
            .field("max_bytes", &self.max_bytes)
            .finish_non_exhaustive()
    }
}

impl FilesystemStorage {
    /// Open the store on `config.dir`: create it if needed, prove it
    /// writable, remove what a crash left in `.tmp/`, and count the bytes
    /// already stored. Any failure refuses (the gateway does not start on a
    /// store it cannot write).
    pub async fn open(config: FilesystemConfig, signer: LinkSigner) -> Result<Self, StorageError> {
        let root = config.dir.clone();
        let opened = tokio::task::spawn_blocking(move || prepare(&root))
            .await
            .map_err(|err| StorageError::Backend(format!("media directory scan: {err}")))??;
        let (root, used) = opened;
        if used > config.max_bytes {
            tracing::warn!(
                used_bytes = used,
                max_bytes = config.max_bytes,
                "the media directory holds more than BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES: \
                 every write is refused until the media GC or an operator frees space, \
                 or the limit is raised"
            );
        }
        Ok(Self {
            root,
            max_bytes: config.max_bytes,
            used: Mutex::new(used),
            signer,
            public_url: config.public_url.trim_end_matches('/').to_string(),
        })
    }

    /// The file of `key` (and its `.meta`), for a key that parses.
    fn paths_of(&self, key: &str) -> Result<(ObjectKey, PathBuf, PathBuf), StorageError> {
        let parsed = ObjectKey::parse(key)
            .map_err(|err| StorageError::Invalid(format!("not a media key: {err}")))?;
        let mut data = self.root.clone();
        for segment in parsed.as_str().split('/') {
            data.push(segment);
        }
        // Belt and braces: what `parse` accepts are plain names.
        if !data
            .strip_prefix(&self.root)
            .map(|rel| {
                rel.components()
                    .all(|c| matches!(c, std::path::Component::Normal(_)))
            })
            .unwrap_or(false)
        {
            return Err(StorageError::Invalid(
                "key leaves the media directory".into(),
            ));
        }
        let mut meta = data.clone().into_os_string();
        meta.push(META_SUFFIX);
        Ok((parsed, data, PathBuf::from(meta)))
    }

    fn lock_used(&self) -> std::sync::MutexGuard<'_, u64> {
        self.used
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Reserve `new` bytes for a write that replaces `old`, or refuse.
    fn reserve(&self, key: &str, new: u64) -> Result<(), StorageError> {
        let mut used = self.lock_used();
        let after = used.saturating_add(new);
        if after > self.max_bytes {
            let message = format!(
                "storing {new} bytes would bring the media directory to {after} of the \
                 {} bytes BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES allows",
                self.max_bytes
            );
            tracing::warn!(key = %key, used_bytes = *used, "media write refused: {message}");
            return Err(StorageError::Full(message));
        }
        *used = after;
        Ok(())
    }

    fn release(&self, bytes: u64) {
        let mut used = self.lock_used();
        *used = used.saturating_sub(bytes);
    }

    /// Write `bytes` to `target` atomically: a temporary file under `.tmp/`,
    /// flushed, renamed into place.
    async fn write_atomic(&self, target: &Path, bytes: &[u8]) -> Result<(), StorageError> {
        let tmp = self
            .root
            .join(TMP_DIR)
            .join(format!("{}.part", ulid::Ulid::new()));
        let result = async {
            let mut file = tokio::fs::File::create(&tmp).await?;
            file.write_all(bytes).await?;
            file.sync_all().await?;
            drop(file);
            if let Some(parent) = target.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }
            tokio::fs::rename(&tmp, target).await
        }
        .await;
        if let Err(err) = result {
            let _ = tokio::fs::remove_file(&tmp).await;
            return Err(io_error(err, "write"));
        }
        Ok(())
    }

    /// Bytes of the stored files.
    pub fn used_bytes(&self) -> u64 {
        *self
            .used
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The limit.
    pub fn max_bytes(&self) -> u64 {
        self.max_bytes
    }

    /// The directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// The file a valid link names. `None` for every link that is not
    /// valid: a key that does not parse, a malformed or past expiry, a
    /// signature that is not this gateway's, a file that is not there.
    pub async fn open_download(
        &self,
        key: &str,
        expires: &str,
        signature: &str,
        now: u64,
    ) -> Result<Option<Download>, StorageError> {
        let Ok(parsed) = ObjectKey::parse(key) else {
            return Ok(None);
        };
        // Digits only: no sign, no space, no `+`.
        if expires.is_empty() || !expires.bytes().all(|b| b.is_ascii_digit()) {
            return Ok(None);
        }
        let Ok(expires) = expires.parse::<u64>() else {
            return Ok(None);
        };
        if !self.signer.verify(&parsed, expires, signature, now) {
            return Ok(None);
        }
        let (_, data, meta) = self.paths_of(parsed.as_str())?;
        let file = match tokio::fs::File::open(&data).await {
            Ok(file) => file,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(err) => return Err(io_error(err, "open")),
        };
        let metadata = file.metadata().await.map_err(|err| io_error(err, "stat"))?;
        if !metadata.is_file() {
            return Ok(None);
        }
        Ok(Some(Download {
            file,
            size_bytes: metadata.len(),
            content_type: read_content_type(&meta).await?,
            file_name: parsed.file_name().to_string(),
            remaining_secs: expires - now,
        }))
    }
}

#[async_trait]
impl Storage for FilesystemStorage {
    async fn put(&self, key: &str, bytes: Bytes, content_type: &str) -> Result<(), StorageError> {
        let (_, data, meta) = self.paths_of(key)?;
        let new = bytes.len() as u64;
        self.reserve(key, new)?;
        let old = file_size(&data).await;
        let written = async {
            self.write_atomic(&meta, content_type.as_bytes()).await?;
            self.write_atomic(&data, &bytes).await
        }
        .await;
        match written {
            // The replaced file's bytes are given back.
            Ok(()) => {
                self.release(old.unwrap_or(0));
                Ok(())
            }
            Err(err) => {
                self.release(new);
                Err(err)
            }
        }
    }

    async fn get(&self, key: &str) -> Result<Bytes, StorageError> {
        let (_, data, _) = self.paths_of(key)?;
        match tokio::fs::read(&data).await {
            Ok(bytes) => Ok(Bytes::from(bytes)),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                Err(StorageError::NotFound { key: key.into() })
            }
            Err(err) => Err(io_error(err, "read")),
        }
    }

    async fn head(&self, key: &str) -> Result<ObjectMetadata, StorageError> {
        let (_, data, meta) = self.paths_of(key)?;
        let metadata = match tokio::fs::metadata(&data).await {
            Ok(m) if m.is_file() => m,
            Ok(_) => return Err(StorageError::NotFound { key: key.into() }),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                return Err(StorageError::NotFound { key: key.into() });
            }
            Err(err) => return Err(io_error(err, "stat")),
        };
        let last_modified_unix_secs = metadata
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0);
        Ok(ObjectMetadata {
            size_bytes: metadata.len(),
            content_type: read_content_type(&meta).await?,
            last_modified_unix_secs,
        })
    }

    async fn delete(&self, key: &str) -> Result<(), StorageError> {
        let (_, data, meta) = self.paths_of(key)?;
        let size = file_size(&data).await;
        match tokio::fs::remove_file(&data).await {
            // Only the delete that removed the file gives its bytes back.
            Ok(()) => self.release(size.unwrap_or(0)),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(io_error(err, "delete")),
        }
        match tokio::fs::remove_file(&meta).await {
            Ok(()) => Ok(()),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(err) => Err(io_error(err, "delete")),
        }
    }

    async fn presigned_get_url(&self, key: &str, ttl: Duration) -> Result<String, StorageError> {
        let (parsed, data, _) = self.paths_of(key)?;
        if file_size(&data).await.is_none() {
            return Err(StorageError::NotFound { key: key.into() });
        }
        let expires = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|err| StorageError::Backend(format!("clock: {err}")))?
            .as_secs()
            .saturating_add(ttl.as_secs());
        let signature = self.signer.sign(&parsed, expires);
        Ok(format!(
            "{}{DOWNLOAD_PREFIX}{}?expires={expires}&signature={signature}",
            self.public_url,
            parsed.as_str()
        ))
    }
}

/// The size of the regular file at `path`, `None` when there is none.
async fn file_size(path: &Path) -> Option<u64> {
    tokio::fs::metadata(path)
        .await
        .ok()
        .filter(|m| m.is_file())
        .map(|m| m.len())
}

/// The Content-Type recorded beside a file. A missing or unreadable record
/// is `application/octet-stream`, which a browser only downloads: the most
/// restrictive answer, never a type the bytes did not have.
async fn read_content_type(meta: &Path) -> Result<String, StorageError> {
    match tokio::fs::read_to_string(meta).await {
        Ok(text) if !text.trim().is_empty() => Ok(text.trim().to_string()),
        Ok(_) => Ok("application/octet-stream".into()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            Ok("application/octet-stream".into())
        }
        Err(err) => Err(io_error(err, "read the content type")),
    }
}

fn io_error(err: std::io::Error, what: &str) -> StorageError {
    match err.kind() {
        std::io::ErrorKind::PermissionDenied => {
            StorageError::PermissionDenied(format!("media directory {what}: {err}"))
        }
        _ => StorageError::Backend(format!("media directory {what}: {err}")),
    }
}

/// Create the directory and `.tmp/`, empty `.tmp/`, prove the directory
/// writable, and count the bytes of the stored files. Blocking.
fn prepare(dir: &Path) -> Result<(PathBuf, u64), StorageError> {
    let fail = |what: &str, err: std::io::Error| {
        StorageError::Backend(format!(
            "media directory {} (BRUTOR_MEDIA_FILESYSTEM_DIR): {what}: {err}",
            dir.display()
        ))
    };
    std::fs::create_dir_all(dir).map_err(|e| fail("cannot create it", e))?;
    let root = std::fs::canonicalize(dir).map_err(|e| fail("cannot resolve it", e))?;
    let tmp = root.join(TMP_DIR);
    if tmp.exists() {
        std::fs::remove_dir_all(&tmp).map_err(|e| fail("cannot empty .tmp", e))?;
    }
    std::fs::create_dir_all(&tmp).map_err(|e| fail("cannot create .tmp", e))?;
    let probe = tmp.join("write-probe");
    std::fs::write(&probe, b"probe").map_err(|e| fail("not writable", e))?;
    std::fs::remove_file(&probe).map_err(|e| fail("not writable", e))?;

    fn count(dir: &Path, skip: &Path) -> std::io::Result<u64> {
        let mut total = 0;
        for entry in std::fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            let kind = entry.file_type()?;
            if kind.is_dir() {
                if path != skip {
                    total += count(&path, skip)?;
                }
            } else if kind.is_file() && !path.to_string_lossy().ends_with(META_SUFFIX) {
                total += entry.metadata()?.len();
            }
        }
        Ok(total)
    }
    let used = count(&root, &tmp).map_err(|e| fail("cannot count its files", e))?;
    Ok((root, used))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "test-jwt-secret-for-the-media-store-tests-0123456789";
    const KEY: &str = "tenant-acme/image/2026/11/file_01HX1234567890ABCDEF.png";
    const OTHER_TENANT_KEY: &str = "tenant-beta/image/2026/11/file_01HX1234567890ABCDEF.png";

    /// A directory of its own under the system's temporary directory,
    /// removed when dropped.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            let dir = std::env::temp_dir().join(format!("brutor-media-test-{}", ulid::Ulid::new()));
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn config(dir: &Path, max_bytes: u64) -> FilesystemConfig {
        FilesystemConfig {
            dir: dir.to_path_buf(),
            max_bytes,
            public_url: "https://gateway.example.com/".into(),
        }
    }

    async fn store(dir: &TempDir, max_bytes: u64) -> FilesystemStorage {
        FilesystemStorage::open(
            config(&dir.0, max_bytes),
            LinkSigner::from_jwt_secret(SECRET),
        )
        .await
        .expect("the store opens")
    }

    fn now() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
    }

    /// Every file under `dir`, relative, sorted.
    fn files_under(dir: &Path) -> Vec<String> {
        fn walk(base: &Path, dir: &Path, out: &mut Vec<String>) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    walk(base, &path, out);
                } else {
                    out.push(
                        path.strip_prefix(base)
                            .unwrap()
                            .to_string_lossy()
                            .replace('\\', "/"),
                    );
                }
            }
        }
        let mut out = Vec::new();
        walk(dir, dir, &mut out);
        out.sort();
        out
    }

    fn query_of(url: &str) -> (String, String, String) {
        let parsed = url::Url::parse(url).unwrap();
        let key = parsed
            .path()
            .strip_prefix(DOWNLOAD_PREFIX)
            .expect("the link is under /v1/media/")
            .to_string();
        let mut expires = String::new();
        let mut signature = String::new();
        for (name, value) in parsed.query_pairs() {
            match name.as_ref() {
                "expires" => expires = value.into_owned(),
                "signature" => signature = value.into_owned(),
                other => panic!("unexpected query parameter {other}"),
            }
        }
        (key, expires, signature)
    }

    #[tokio::test]
    async fn put_then_get_head_and_delete_round_trip() {
        let dir = TempDir::new();
        let s = store(&dir, 1_000).await;
        s.put(KEY, Bytes::from_static(b"hello"), "image/png")
            .await
            .unwrap();
        assert_eq!(s.get(KEY).await.unwrap().as_ref(), b"hello");
        let meta = s.head(KEY).await.unwrap();
        assert_eq!(meta.size_bytes, 5);
        assert_eq!(meta.content_type, "image/png");
        assert!(meta.last_modified_unix_secs > 0);
        assert_eq!(s.used_bytes(), 5);

        s.delete(KEY).await.unwrap();
        assert!(matches!(
            s.get(KEY).await,
            Err(StorageError::NotFound { .. })
        ));
        assert!(matches!(
            s.head(KEY).await,
            Err(StorageError::NotFound { .. })
        ));
        // Idempotent: the GC sweep relies on it.
        s.delete(KEY).await.unwrap();
        assert_eq!(s.used_bytes(), 0);
    }

    /// The file is the key under the directory: the tenant's own directory,
    /// as the tenant's own prefix in a bucket. Nothing is left in `.tmp/`.
    #[tokio::test]
    async fn each_tenant_has_its_own_directory_and_writes_leave_no_temporary_file() {
        let dir = TempDir::new();
        let s = store(&dir, 1_000).await;
        s.put(KEY, Bytes::from_static(b"a"), "image/png")
            .await
            .unwrap();
        s.put(OTHER_TENANT_KEY, Bytes::from_static(b"bb"), "image/png")
            .await
            .unwrap();
        assert_eq!(
            files_under(&dir.0),
            vec![
                format!("{KEY}"),
                format!("{KEY}{META_SUFFIX}"),
                format!("{OTHER_TENANT_KEY}"),
                format!("{OTHER_TENANT_KEY}{META_SUFFIX}"),
            ]
        );
        assert_eq!(s.get(OTHER_TENANT_KEY).await.unwrap().as_ref(), b"bb");
    }

    #[tokio::test]
    async fn an_overwrite_replaces_bytes_and_content_type_and_counts_once() {
        let dir = TempDir::new();
        let s = store(&dir, 1_000).await;
        s.put(KEY, Bytes::from_static(b"v1"), "text/plain")
            .await
            .unwrap();
        s.put(KEY, Bytes::from_static(b"v2-longer"), "image/png")
            .await
            .unwrap();
        let meta = s.head(KEY).await.unwrap();
        assert_eq!(meta.size_bytes, 9);
        assert_eq!(meta.content_type, "image/png");
        assert_eq!(s.get(KEY).await.unwrap().as_ref(), b"v2-longer");
        assert_eq!(s.used_bytes(), 9);
    }

    /// A write that would pass the limit is refused, names the setting, and
    /// writes nothing; a delete (the media GC) makes room again.
    #[tokio::test]
    async fn a_write_past_the_size_limit_is_refused_and_a_delete_makes_room() {
        let dir = TempDir::new();
        let s = store(&dir, 10).await;
        s.put(KEY, Bytes::from_static(b"123456"), "image/png")
            .await
            .unwrap();
        let err = s
            .put(OTHER_TENANT_KEY, Bytes::from_static(b"12345"), "image/png")
            .await
            .unwrap_err();
        assert!(matches!(err, StorageError::Full(_)), "{err:?}");
        assert!(
            err.to_string()
                .contains("BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES"),
            "{err}"
        );
        assert!(matches!(
            s.get(OTHER_TENANT_KEY).await,
            Err(StorageError::NotFound { .. })
        ));
        assert_eq!(s.used_bytes(), 6);
        // Exactly the limit is accepted.
        s.put(OTHER_TENANT_KEY, Bytes::from_static(b"1234"), "image/png")
            .await
            .unwrap();
        assert_eq!(s.used_bytes(), 10);
        s.delete(KEY).await.unwrap();
        s.put(KEY, Bytes::from_static(b"123456"), "image/png")
            .await
            .unwrap();
        assert_eq!(s.used_bytes(), 10);
    }

    /// Concurrent writes cannot pass the limit together: each reserves
    /// before it writes.
    #[tokio::test]
    async fn concurrent_writes_never_pass_the_limit_together() {
        let dir = TempDir::new();
        let s = std::sync::Arc::new(store(&dir, 50).await);
        let mut tasks = Vec::new();
        for i in 0..20 {
            let s = s.clone();
            tasks.push(tokio::spawn(async move {
                let key = format!("tenant-acme/image/2026/11/file_concurrent{i:04}.png");
                s.put(&key, Bytes::from(vec![0u8; 10]), "image/png").await
            }));
        }
        let mut stored = 0;
        for task in tasks {
            match task.await.unwrap() {
                Ok(()) => stored += 1,
                Err(StorageError::Full(_)) => {}
                Err(other) => panic!("{other:?}"),
            }
        }
        assert_eq!(stored, 5);
        assert_eq!(s.used_bytes(), 50);
    }

    /// The total is counted from the directory at start, and what a crash
    /// left in `.tmp/` is removed then.
    #[tokio::test]
    async fn a_restart_counts_the_stored_bytes_and_removes_unfinished_writes() {
        let dir = TempDir::new();
        {
            let s = store(&dir, 100).await;
            s.put(KEY, Bytes::from_static(b"1234567"), "image/png")
                .await
                .unwrap();
        }
        std::fs::write(dir.0.join(TMP_DIR).join("left-by-a-crash"), b"xxxxxxxxxxxx").unwrap();
        let s = store(&dir, 100).await;
        assert_eq!(s.used_bytes(), 7);
        assert!(!dir.0.join(TMP_DIR).join("left-by-a-crash").exists());
        assert_eq!(s.get(KEY).await.unwrap().as_ref(), b"1234567");
    }

    #[tokio::test]
    async fn the_store_refuses_to_open_on_a_directory_it_cannot_create() {
        let dir = TempDir::new();
        let file = dir.0.join("a-file");
        std::fs::write(&file, b"x").unwrap();
        let err = FilesystemStorage::open(
            config(&file.join("media"), 100),
            LinkSigner::from_jwt_secret(SECRET),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, StorageError::Backend(_)), "{err:?}");
    }

    /// Keys come from `ObjectKey::build`; anything else, path traversal
    /// first, is refused by every operation and touches nothing outside
    /// the directory.
    #[tokio::test]
    async fn traversal_keys_are_refused_by_every_operation() {
        let dir = TempDir::new();
        let root = dir.0.join("media");
        let s = FilesystemStorage::open(config(&root, 1_000), LinkSigner::from_jwt_secret(SECRET))
            .await
            .unwrap();
        let outside = dir.0.join("outside.txt");
        std::fs::write(&outside, b"secret").unwrap();
        for bad in [
            "../outside.txt",
            "tenant-acme/image/2026/11/../../../../../outside.txt",
            "/etc/passwd",
            "tenant-acme/../../outside.txt",
            "tenant-acme/image/2026/11/file_abcdefgh.png/../../../../../../outside.txt",
            "tenant-acme",
            "tenant-acme/image/2026/11",
            ".tmp/x",
            "k1",
            "",
        ] {
            assert!(
                matches!(
                    s.put(bad, Bytes::from_static(b"x"), "text/plain").await,
                    Err(StorageError::Invalid(_))
                ),
                "put {bad:?}"
            );
            assert!(
                matches!(s.get(bad).await, Err(StorageError::Invalid(_))),
                "get {bad:?}"
            );
            assert!(
                matches!(s.head(bad).await, Err(StorageError::Invalid(_))),
                "head {bad:?}"
            );
            assert!(
                matches!(s.delete(bad).await, Err(StorageError::Invalid(_))),
                "delete {bad:?}"
            );
            assert!(
                matches!(
                    s.presigned_get_url(bad, Duration::from_secs(60)).await,
                    Err(StorageError::Invalid(_))
                ),
                "presign {bad:?}"
            );
            assert!(
                s.open_download(bad, &(now() + 60).to_string(), "00", now())
                    .await
                    .unwrap()
                    .is_none(),
                "download {bad:?}"
            );
        }
        assert_eq!(std::fs::read(&outside).unwrap(), b"secret");
        assert!(files_under(&root).is_empty(), "{:?}", files_under(&root));
    }

    /// The link the store hands out opens the file, with its Content-Type
    /// and the seconds it has left; the link is under the public URL.
    #[tokio::test]
    async fn a_signed_link_opens_its_file() {
        let dir = TempDir::new();
        let s = store(&dir, 1_000).await;
        s.put(KEY, Bytes::from_static(b"png-bytes"), "image/png")
            .await
            .unwrap();
        let url = s
            .presigned_get_url(KEY, Duration::from_secs(300))
            .await
            .unwrap();
        assert!(
            url.starts_with(&format!(
                "https://gateway.example.com{DOWNLOAD_PREFIX}{KEY}?"
            )),
            "{url}"
        );
        let (key, expires, signature) = query_of(&url);
        let download = s
            .open_download(&key, &expires, &signature, now())
            .await
            .unwrap()
            .expect("the link opens");
        assert_eq!(download.size_bytes, 9);
        assert_eq!(download.content_type, "image/png");
        assert_eq!(download.file_name, "file_01HX1234567890ABCDEF.png");
        assert!(
            (290..=300).contains(&download.remaining_secs),
            "{}",
            download.remaining_secs
        );
    }

    #[tokio::test]
    async fn a_link_for_a_missing_object_is_not_minted() {
        let dir = TempDir::new();
        let s = store(&dir, 1_000).await;
        assert!(matches!(
            s.presigned_get_url(KEY, Duration::from_secs(60)).await,
            Err(StorageError::NotFound { .. })
        ));
    }

    /// Expired, tampered (another key, another tenant, another expiry, a
    /// changed signature), malformed and unknown links open nothing.
    #[tokio::test]
    async fn expired_tampered_and_unknown_links_open_nothing() {
        let dir = TempDir::new();
        let s = store(&dir, 1_000).await;
        s.put(KEY, Bytes::from_static(b"a"), "image/png")
            .await
            .unwrap();
        s.put(OTHER_TENANT_KEY, Bytes::from_static(b"b"), "image/png")
            .await
            .unwrap();
        let url = s
            .presigned_get_url(KEY, Duration::from_secs(60))
            .await
            .unwrap();
        let (key, expires, signature) = query_of(&url);
        let at = now();
        let opens = |key: String, expires: String, signature: String, at: u64| {
            let s = &s;
            async move {
                s.open_download(&key, &expires, &signature, at)
                    .await
                    .unwrap()
                    .is_some()
            }
        };
        assert!(opens(key.clone(), expires.clone(), signature.clone(), at).await);

        // Expired: one second after its expiry.
        let past = expires.parse::<u64>().unwrap() + 1;
        assert!(!opens(key.clone(), expires.clone(), signature.clone(), past).await);
        // Another tenant's file under this link's signature.
        assert!(
            !opens(
                OTHER_TENANT_KEY.into(),
                expires.clone(),
                signature.clone(),
                at
            )
            .await
        );
        // A longer life.
        let longer = (expires.parse::<u64>().unwrap() + 3600).to_string();
        assert!(!opens(key.clone(), longer, signature.clone(), at).await);
        // One hex digit changed.
        let mut flipped = signature.clone().into_bytes();
        flipped[0] = if flipped[0] == b'0' { b'1' } else { b'0' };
        assert!(
            !opens(
                key.clone(),
                expires.clone(),
                String::from_utf8(flipped).unwrap(),
                at
            )
            .await
        );
        // Malformed.
        for (e, sig) in [
            ("", signature.as_str()),
            ("soon", signature.as_str()),
            ("-1", signature.as_str()),
            (expires.as_str(), ""),
            (expires.as_str(), "zz"),
            (expires.as_str(), &signature[..signature.len() - 2]),
        ] {
            assert!(
                !opens(key.clone(), e.to_string(), sig.to_string(), at).await,
                "{e:?} {sig:?}"
            );
        }
        // A signature of another gateway's secret.
        let foreign = LinkSigner::from_jwt_secret("another-gateway-secret-0123456789abcdef");
        let parsed = ObjectKey::parse(&key).unwrap();
        let exp: u64 = expires.parse().unwrap();
        assert!(!opens(key.clone(), expires.clone(), foreign.sign(&parsed, exp), at).await);
        // A valid signature for a file that is not there (deleted).
        s.delete(KEY).await.unwrap();
        assert!(!opens(key, expires, signature, at).await);
    }

    #[test]
    fn the_signature_covers_tenant_key_and_expiry_and_depends_on_the_secret() {
        let a = LinkSigner::from_jwt_secret(SECRET);
        let b = LinkSigner::from_jwt_secret("another-gateway-secret-0123456789abcdef");
        let key = ObjectKey::parse(KEY).unwrap();
        let other = ObjectKey::parse(OTHER_TENANT_KEY).unwrap();
        let sig = a.sign(&key, 1_000);
        assert_eq!(sig.len(), 64);
        assert!(sig.chars().all(|c| c.is_ascii_hexdigit()));
        assert!(a.verify(&key, 1_000, &sig, 999));
        assert!(a.verify(&key, 1_000, &sig, 1_000));
        assert!(!a.verify(&key, 1_000, &sig, 1_001), "expired");
        assert!(!a.verify(&key, 1_001, &sig, 999), "another expiry");
        assert!(!a.verify(&other, 1_000, &sig, 999), "another tenant");
        assert!(!b.verify(&key, 1_000, &sig, 999), "another secret");
        assert_ne!(a.sign(&key, 1_000), b.sign(&key, 1_000));
        assert_ne!(a.sign(&key, 1_000), a.sign(&other, 1_000));
        // The derived key is not the secret itself.
        assert_ne!(a.key.as_slice(), &SECRET.as_bytes()[..32]);
    }
}
