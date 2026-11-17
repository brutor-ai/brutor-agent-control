// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Pure object-key composition.
//!
//! Path layout (matches the value stored in `media_files.object_key`):
//!
//! ```text
//! ${tenant_id}/${kind}/${yyyy}/${mm}/${file_id}.${ext}
//! ```
//!
//! The leading tenant_id segment is the single isolation control
//! the bucket-policy SCP relies on — a typo here defeats every
//! cross-tenant guarantee. So we don't compose paths with
//! `format!`; we go through [`ObjectKey::build`] which validates
//! every segment.
//!
//! Validation rules:
//!
//! - **tenant_id**: ASCII alphanumeric + `-` + `_`, length 2..=255.
//!   Rejects `..`, `/`, `\`, leading/trailing dot. Matches the
//!   `Tenant.validate_name` rules in gateway-control so an admin
//!   can't create a tenant whose id makes us emit a path the
//!   bucket policy can't grasp.
//! - **kind**: enum (no free-form strings).
//! - **file_id**: 8..=64 ASCII chars, alphanumeric + `_`. ULIDs and
//!   the `file_<ulid>` shape both fit.
//! - **ext**: lowercase ASCII alphanumeric, length 1..=8. No `.`,
//!   no `/`. Caller passes the extension *without* the leading
//!   dot.
//! - **date components**: derived from a `chrono::DateTime<Utc>`
//!   the caller supplies — we don't accept yyyy/mm strings to
//!   prevent path-traversal via injected date values.

use chrono::{DateTime, Datelike, Utc};
use std::fmt;

/// Asset class. Mirrors `MediaFile.kind` in
/// `app/database/models/media_file.py`.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub enum MediaKind {
    Image,
    AudioSpeech,
    AudioTranscriptionInput,
    Video,
    Document,
    Other,
}

impl MediaKind {
    /// Wire/storage representation. Stable — chat messages and
    /// `media_files.kind` rows hold this string verbatim.
    pub const fn as_str(self) -> &'static str {
        match self {
            MediaKind::Image => "image",
            MediaKind::AudioSpeech => "audio_speech",
            MediaKind::AudioTranscriptionInput => "audio_transcription_input",
            MediaKind::Video => "video",
            MediaKind::Document => "document",
            MediaKind::Other => "other",
        }
    }

    /// Parse a wire/storage string back to the enum. Used when we
    /// load a `media_files` row and need to re-derive the path
    /// segment.
    pub fn from_str_strict(s: &str) -> Option<Self> {
        Some(match s {
            "image" => MediaKind::Image,
            "audio_speech" => MediaKind::AudioSpeech,
            "audio_transcription_input" => MediaKind::AudioTranscriptionInput,
            "video" => MediaKind::Video,
            "document" => MediaKind::Document,
            "other" => MediaKind::Other,
            _ => return None,
        })
    }
}

impl fmt::Display for MediaKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Errors from [`ObjectKey::build`]. Each variant maps to a
/// validation rule documented at the module level.
#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
pub enum KeyError {
    #[error("invalid tenant_id: {0}")]
    InvalidTenantId(String),
    #[error("invalid file_id: {0}")]
    InvalidFileId(String),
    #[error("invalid extension: {0}")]
    InvalidExtension(String),
    /// A key read back ([`ObjectKey::parse`]) that is not of the layout
    /// [`ObjectKey::build`] writes.
    #[error("invalid key layout: {0}")]
    InvalidLayout(String),
}

/// A validated bucket-relative key. Only constructed via
/// [`ObjectKey::build`] — callers can't bypass validation by
/// instantiating the inner string directly.
#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct ObjectKey(String);

impl ObjectKey {
    /// Build a key from its components. Returns the validated path
    /// or a [`KeyError`] explaining why the input was rejected.
    pub fn build(
        tenant_id: &str,
        kind: MediaKind,
        file_id: &str,
        ext: &str,
        when: DateTime<Utc>,
    ) -> Result<Self, KeyError> {
        validate_tenant_id(tenant_id)?;
        validate_file_id(file_id)?;
        validate_ext(ext)?;

        let yyyy = when.year();
        let mm = when.month(); // 1..=12
        let key = format!(
            "{tenant_id}/{kind}/{yyyy:04}/{mm:02}/{file_id}.{ext}",
            kind = kind.as_str(),
        );
        Ok(ObjectKey(key))
    }

    /// The validated key string. Pass this to your storage backend.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Take ownership of the underlying string. Useful when the
    /// storage backend's API takes `String`.
    pub fn into_string(self) -> String {
        self.0
    }

    /// The key `raw` when it is one [`ObjectKey::build`] could have made:
    /// `tenant/kind/yyyy/mm/file_id.ext`, every segment validated as `build`
    /// validates it. What a store that maps keys to files on disk accepts,
    /// and what a signed download link names: no segment of a key that
    /// parses can be `..`, `.`, empty, absolute or hold a separator, so it
    /// cannot name anything outside the tenant's own directory.
    pub fn parse(raw: &str) -> Result<Self, KeyError> {
        let segments: Vec<&str> = raw.split('/').collect();
        let [tenant_id, kind, yyyy, mm, file_name] = segments.as_slice() else {
            return Err(KeyError::InvalidLayout(format!(
                "{} segments, not tenant/kind/yyyy/mm/file",
                segments.len()
            )));
        };
        validate_tenant_id(tenant_id)?;
        if MediaKind::from_str_strict(kind).is_none() {
            return Err(KeyError::InvalidLayout("unknown kind".into()));
        }
        let digits = |s: &str, n: usize| s.len() == n && s.bytes().all(|b| b.is_ascii_digit());
        if !digits(yyyy, 4) {
            return Err(KeyError::InvalidLayout("year is not 4 digits".into()));
        }
        if !digits(mm, 2) || !(1..=12).contains(&mm.parse::<u32>().unwrap_or(0)) {
            return Err(KeyError::InvalidLayout("month is not 01..12".into()));
        }
        let Some((file_id, ext)) = file_name.split_once('.') else {
            return Err(KeyError::InvalidLayout("file name has no extension".into()));
        };
        validate_file_id(file_id)?;
        validate_ext(ext)?;
        Ok(ObjectKey(raw.to_string()))
    }

    /// The last segment: `file_id.ext`.
    pub fn file_name(&self) -> &str {
        self.0.rsplit('/').next().unwrap_or("")
    }

    /// Recover the leading tenant prefix. Used by the bucket-policy
    /// audit and by `presigned_get_url` to double-check the caller's
    /// tenant claim matches the key the URL is being minted for.
    pub fn tenant_prefix(&self) -> &str {
        // Always present — we built it from a validated tenant_id.
        self.0.split('/').next().unwrap_or("")
    }
}

impl fmt::Display for ObjectKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

// ---------------------------------------------------------------------------
// Validators (private — the entry points are `ObjectKey::build` and `ObjectKey::parse`)
// ---------------------------------------------------------------------------

fn validate_tenant_id(s: &str) -> Result<(), KeyError> {
    let len = s.len();
    if !(2..=255).contains(&len) {
        return Err(KeyError::InvalidTenantId(format!(
            "length {len} not in 2..=255"
        )));
    }
    if s.starts_with('.') || s.ends_with('.') {
        return Err(KeyError::InvalidTenantId("leading/trailing '.'".into()));
    }
    if !s
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(KeyError::InvalidTenantId(format!(
            "{s:?} contains characters outside [a-zA-Z0-9_-]"
        )));
    }
    Ok(())
}

fn validate_file_id(s: &str) -> Result<(), KeyError> {
    let len = s.len();
    if !(8..=64).contains(&len) {
        return Err(KeyError::InvalidFileId(format!(
            "length {len} not in 8..=64"
        )));
    }
    if !s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return Err(KeyError::InvalidFileId(format!(
            "{s:?} contains characters outside [a-zA-Z0-9_]"
        )));
    }
    Ok(())
}

fn validate_ext(s: &str) -> Result<(), KeyError> {
    let len = s.len();
    if !(1..=8).contains(&len) {
        return Err(KeyError::InvalidExtension(format!(
            "length {len} not in 1..=8"
        )));
    }
    if !s
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    {
        return Err(KeyError::InvalidExtension(format!(
            "{s:?} contains characters outside [a-z0-9]"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn dt(y: i32, m: u32, d: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, m, d, 0, 0, 0).unwrap()
    }

    #[test]
    fn build_happy_path() {
        let key = ObjectKey::build(
            "tenant-acme",
            MediaKind::Image,
            "file_01HX1234567890ABCDEF",
            "png",
            dt(2026, 4, 30),
        )
        .unwrap();
        assert_eq!(
            key.as_str(),
            "tenant-acme/image/2026/04/file_01HX1234567890ABCDEF.png"
        );
        assert_eq!(key.tenant_prefix(), "tenant-acme");
    }

    #[test]
    fn rejects_path_traversal_in_tenant_id() {
        for bad in ["../other", "./me", "a/b", "a\\b", "..", "."] {
            let err = ObjectKey::build(
                bad,
                MediaKind::Image,
                "file_abcdefgh",
                "png",
                dt(2026, 4, 30),
            )
            .unwrap_err();
            assert!(
                matches!(err, KeyError::InvalidTenantId(_)),
                "expected InvalidTenantId for {bad:?}, got {err:?}"
            );
        }
    }

    #[test]
    fn rejects_path_traversal_in_extension() {
        for bad in ["../png", "p/n", "PNG", "p.ng", ".png", ""] {
            let err = ObjectKey::build(
                "tenant-acme",
                MediaKind::Image,
                "file_abcdefgh",
                bad,
                dt(2026, 4, 30),
            )
            .unwrap_err();
            assert!(
                matches!(err, KeyError::InvalidExtension(_)),
                "expected InvalidExtension for {bad:?}, got {err:?}"
            );
        }
    }

    #[test]
    fn rejects_short_or_invalid_file_id() {
        for bad in [
            "short",
            "file with space",
            "file/slash",
            "file.dot",
            "",
            // exactly 7 chars — below the 8-char floor
            "file_12",
        ] {
            let err =
                ObjectKey::build("tenant-acme", MediaKind::Image, bad, "png", dt(2026, 4, 30))
                    .unwrap_err();
            assert!(
                matches!(err, KeyError::InvalidFileId(_)),
                "expected InvalidFileId for {bad:?}, got {err:?}"
            );
        }
    }

    #[test]
    fn pads_month_zero_padded() {
        let key = ObjectKey::build(
            "t",
            MediaKind::Image,
            "file_abcdefgh",
            "png",
            dt(2026, 1, 5),
        );
        // tenant "t" is 1 char — below the 2-char floor, expect rejection
        assert!(matches!(key, Err(KeyError::InvalidTenantId(_))));

        let key = ObjectKey::build(
            "tt",
            MediaKind::Image,
            "file_abcdefgh",
            "png",
            dt(2026, 1, 5),
        )
        .unwrap();
        assert!(key.as_str().contains("/2026/01/"), "key = {}", key.as_str());
    }

    #[test]
    fn kind_enum_round_trips() {
        for k in [
            MediaKind::Image,
            MediaKind::AudioSpeech,
            MediaKind::AudioTranscriptionInput,
            MediaKind::Video,
            MediaKind::Document,
            MediaKind::Other,
        ] {
            assert_eq!(MediaKind::from_str_strict(k.as_str()), Some(k));
        }
    }

    #[test]
    fn kind_enum_rejects_unknown_strings() {
        for bad in ["", "Image", "image_extra", "audiospeech"] {
            assert_eq!(
                MediaKind::from_str_strict(bad),
                None,
                "expected None for {bad:?}"
            );
        }
    }

    #[test]
    fn a_key_build_makes_parses_back_to_itself() {
        let built = ObjectKey::build(
            "tenant-acme",
            MediaKind::AudioTranscriptionInput,
            "file_01HX1234567890ABCDEF",
            "webm",
            dt(2026, 11, 16),
        )
        .unwrap();
        let parsed = ObjectKey::parse(built.as_str()).unwrap();
        assert_eq!(parsed, built);
        assert_eq!(parsed.tenant_prefix(), "tenant-acme");
        assert_eq!(parsed.file_name(), "file_01HX1234567890ABCDEF.webm");
    }

    /// Path traversal and every other shape `build` never writes: refused.
    #[test]
    fn parse_refuses_traversal_and_every_other_layout() {
        for bad in [
            "",
            "/",
            "../etc/passwd",
            "/etc/passwd",
            "tenant-acme/image/2026/04/../../../../etc/passwd",
            "tenant-acme/image/2026/04/..",
            "tenant-acme/image/2026/04/.",
            "tenant-acme/image/2026/04/file_abcdefgh.png/..",
            "tenant-acme/image/2026/04/file_abcdefgh.png/",
            "tenant-acme/../2026/04/file_abcdefgh.png",
            "../image/2026/04/file_abcdefgh.png",
            "tenant-acme/image/2026/../file_abcdefgh.png",
            "tenant-acme/image/2026/04/..%2f..%2fetc%2fpasswd",
            "tenant-acme/image/2026/04/file_abcdefgh.png\\..\\x",
            "tenant-acme\\image\\2026\\04\\file_abcdefgh.png",
            "tenant-acme//image/2026/04/file_abcdefgh.png",
            "./tenant-acme/image/2026/04/file_abcdefgh.png",
            "tenant-acme/image/2026/04",
            "tenant-acme/image/2026/04/",
            "tenant-acme/image/2026",
            "tenant-acme",
            "tenant-acme/image/2026/04/file_abcdefgh",
            "tenant-acme/image/2026/04/file_abcdefgh.",
            "tenant-acme/image/2026/04/.png",
            "tenant-acme/image/2026/04/file_abcdefgh.png.meta",
            "tenant-acme/image/2026/04/file_abcdefgh.PNG",
            "tenant-acme/images/2026/04/file_abcdefgh.png",
            "tenant-acme/image/26/04/file_abcdefgh.png",
            "tenant-acme/image/2026/4/file_abcdefgh.png",
            "tenant-acme/image/2026/13/file_abcdefgh.png",
            "tenant-acme/image/2026/00/file_abcdefgh.png",
            "tenant-acme/image/2026/04/x/file_abcdefgh.png",
            "tenant-acme/image/2026/04/file_abcdefgh.png\0",
            "C:/image/2026/04/file_abcdefgh.png",
        ] {
            assert!(ObjectKey::parse(bad).is_err(), "{bad:?} parsed as a key");
        }
    }

    #[test]
    fn tenant_prefix_extracts_correctly() {
        let key = ObjectKey::build(
            "acme-corp",
            MediaKind::Video,
            "file_abcdefgh",
            "mp4",
            dt(2026, 12, 31),
        )
        .unwrap();
        assert_eq!(key.tenant_prefix(), "acme-corp");
    }
}
