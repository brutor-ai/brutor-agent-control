// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Custom serde codec for [`Part`] — proto3-JSON oneof encoding.
//!
//! proto3-JSON encodes a `oneof` by promoting the selected field to a
//! top-level key in the enclosing message. For [`Part`], that means
//! one of `text`/`raw`/`url`/`data` appears alongside the shared
//! fields `filename`, `mediaType`, and `metadata`. Exactly one
//! variant MUST be set; zero or multiple is a protocol error.
//!
//! ## Serialization invariants
//!
//! * `raw` bytes are base64-encoded (standard, not URL-safe) per the
//!   proto3-JSON spec for `bytes`.
//! * Missing `filename`/`mediaType`/`metadata` are omitted rather
//!   than emitted as `null`. Empty `metadata` objects are also
//!   omitted.
//! * Unknown sibling fields fail deserialization — strict wire check
//!   protects against partner drift silently dropping our data.

use std::fmt;

use base64::{Engine as _, engine::general_purpose::STANDARD as B64};
use serde::de::{Error as _, MapAccess, Visitor};
use serde::ser::{SerializeMap, Serializer};
use serde::{Deserialize, Deserializer, Serialize};

use super::{Part, PartContent};

impl Serialize for Part {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        // Count the fields up-front so serde JSON can pre-allocate its
        // map (cheap optimisation, but matters for streaming serializers).
        let mut fields = 1; // content discriminant
        if self.metadata.as_ref().map_or(false, |m| !m.is_empty()) {
            fields += 1;
        }
        if self.filename.is_some() {
            fields += 1;
        }
        if self.media_type.is_some() {
            fields += 1;
        }
        let mut map = s.serialize_map(Some(fields))?;

        match &self.content {
            PartContent::Text(t) => map.serialize_entry("text", t)?,
            PartContent::Raw(b) => {
                let encoded = B64.encode(b);
                map.serialize_entry("raw", &encoded)?;
            }
            PartContent::Url(u) => map.serialize_entry("url", u)?,
            PartContent::Data(d) => map.serialize_entry("data", d)?,
        }

        if let Some(md) = &self.metadata {
            if !md.is_empty() {
                map.serialize_entry("metadata", md)?;
            }
        }
        if let Some(f) = &self.filename {
            map.serialize_entry("filename", f)?;
        }
        if let Some(mt) = &self.media_type {
            map.serialize_entry("mediaType", mt)?;
        }
        map.end()
    }
}

impl<'de> Deserialize<'de> for Part {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Part, D::Error> {
        d.deserialize_map(PartVisitor)
    }
}

struct PartVisitor;

impl<'de> Visitor<'de> for PartVisitor {
    type Value = Part;

    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("an A2A v1.0 Part object with exactly one of text/raw/url/data")
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Part, M::Error> {
        // Collect the oneof variants optimistically; at the end we
        // assert exactly one was set.
        let mut text: Option<String> = None;
        let mut raw: Option<Vec<u8>> = None;
        let mut url: Option<String> = None;
        let mut data: Option<serde_json::Value> = None;
        let mut metadata: Option<serde_json::Map<String, serde_json::Value>> = None;
        let mut filename: Option<String> = None;
        let mut media_type: Option<String> = None;
        // Optional JSON-Schema-friendly discriminator. Strict
        // proto3-JSON oneof encoding doesn't include it, but the
        // upstream a2a-sdk reference clients (and most A2A v1.0
        // sample servers) emit it alongside the variant field, e.g.
        // ``{"kind": "text", "text": "hello"}``. We tolerate it on
        // input and validate it agrees with the variant actually
        // supplied so the wire stays unambiguous.
        let mut kind: Option<String> = None;

        while let Some(key) = map.next_key::<String>()? {
            match key.as_str() {
                "text" => {
                    if text.is_some() {
                        return Err(M::Error::duplicate_field("text"));
                    }
                    text = Some(map.next_value()?);
                }
                "raw" => {
                    if raw.is_some() {
                        return Err(M::Error::duplicate_field("raw"));
                    }
                    let encoded: String = map.next_value()?;
                    let decoded = B64.decode(encoded.as_bytes()).map_err(|e| {
                        M::Error::custom(format!("Part.raw base64 decode failed: {e}"))
                    })?;
                    raw = Some(decoded);
                }
                "url" => {
                    if url.is_some() {
                        return Err(M::Error::duplicate_field("url"));
                    }
                    url = Some(map.next_value()?);
                }
                "data" => {
                    if data.is_some() {
                        return Err(M::Error::duplicate_field("data"));
                    }
                    data = Some(map.next_value()?);
                }
                "metadata" => {
                    if metadata.is_some() {
                        return Err(M::Error::duplicate_field("metadata"));
                    }
                    metadata = Some(map.next_value()?);
                }
                "filename" => {
                    if filename.is_some() {
                        return Err(M::Error::duplicate_field("filename"));
                    }
                    filename = Some(map.next_value()?);
                }
                "mediaType" => {
                    if media_type.is_some() {
                        return Err(M::Error::duplicate_field("mediaType"));
                    }
                    media_type = Some(map.next_value()?);
                }
                "kind" => {
                    // Optional discriminator emitted by upstream
                    // reference clients. Recorded for
                    // post-validation against the actual variant
                    // present.
                    if kind.is_some() {
                        return Err(M::Error::duplicate_field("kind"));
                    }
                    kind = Some(map.next_value()?);
                }
                // Reject unknown sibling fields. We don't use
                // #[serde(deny_unknown_fields)] on the struct because
                // we have a custom Visitor; replicate the intent here.
                other => {
                    return Err(M::Error::unknown_field(
                        other,
                        &[
                            "text",
                            "raw",
                            "url",
                            "data",
                            "kind",
                            "metadata",
                            "filename",
                            "mediaType",
                        ],
                    ));
                }
            }
        }

        // Enforce "exactly one of".
        let set = [text.is_some(), raw.is_some(), url.is_some(), data.is_some()];
        let count = set.iter().filter(|b| **b).count();
        let content = match (count, text, raw, url, data) {
            (1, Some(t), _, _, _) => PartContent::Text(t),
            (1, _, Some(b), _, _) => PartContent::Raw(b),
            (1, _, _, Some(u), _) => PartContent::Url(u),
            (1, _, _, _, Some(d)) => PartContent::Data(d),
            (0, _, _, _, _) => {
                return Err(M::Error::custom(
                    "Part must set exactly one of text/raw/url/data (none supplied)",
                ));
            }
            _ => {
                return Err(M::Error::custom(
                    "Part must set exactly one of text/raw/url/data (multiple supplied)",
                ));
            }
        };

        // If the caller supplied the optional `kind` discriminator,
        // it must agree with the variant actually present. Catches
        // accidental drift between client serializer and the
        // payload's oneof field.
        if let Some(k) = kind.as_deref() {
            let variant = match &content {
                PartContent::Text(_) => "text",
                PartContent::Raw(_) => "raw",
                PartContent::Url(_) => "url",
                PartContent::Data(_) => "data",
            };
            if !k.eq_ignore_ascii_case(variant) {
                return Err(M::Error::custom(format!(
                    "Part.kind={k:?} does not match the variant supplied ({variant:?})",
                )));
            }
        }

        Ok(Part {
            content,
            metadata,
            filename,
            media_type,
        })
    }
}
