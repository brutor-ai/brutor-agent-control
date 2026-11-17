// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The trust set: which Ed25519 keys (kids) a relying party accepts.
//!
//! Sources, combinable:
//!
//! * a `/.well-known/brutor-evidence-keys.json` document —
//!   `{"issuer", "keys": [{"kid": <64 hex>, "kty": "OKP", "crv": "Ed25519",
//!   "x": <base64url of the same 32 bytes>, "status": "active"|"retired",
//!   "scope": "deployment"|"tenant", "log_id"?}]}`; `kid` is the lowercase
//!   hex of the raw public key, and `x`, when present, must be the same key;
//! * raw keys as 64-hex strings (`--key`).
//!
//! Retired keys stay trusted (they signed what they signed); reports note
//! them with `kid_retired`. An empty source list means *unpinned*.

use base64::Engine as _;
use serde_json::Value;
use std::collections::{BTreeMap, HashSet};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyInfo {
    pub status: String,
    pub scope: Option<String>,
    pub log_id: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TrustedKeys {
    /// `None` = unpinned.
    kids: Option<BTreeMap<String, KeyInfo>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KidStatus {
    /// In the trusted set; `retired` when the source says so.
    Trusted {
        retired: bool,
    },
    Untrusted,
    /// No trust set at all.
    Unpinned,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyError(pub String);

impl std::fmt::Display for KeyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for KeyError {}

fn hex32(s: &str) -> Result<String, KeyError> {
    let k = s.trim().to_ascii_lowercase();
    let ok = k.len() == 64 && hex::decode(&k).is_ok();
    if !ok {
        return Err(KeyError(format!(
            "{s:?} is not a 32-byte Ed25519 public key in hex"
        )));
    }
    let raw: [u8; 32] = hex::decode(&k)
        .ok()
        .and_then(|b| b.try_into().ok())
        .ok_or_else(|| KeyError(format!("{s:?} is not 32 bytes")))?;
    ed25519_dalek::VerifyingKey::from_bytes(&raw)
        .map_err(|_| KeyError(format!("{s:?} is not a valid Ed25519 point")))?;
    Ok(k)
}

impl TrustedKeys {
    /// No trust set: signatures are checked under the key each statement
    /// names, and reports carry `kid_not_pinned`.
    pub fn unpinned() -> Self {
        Self { kids: None }
    }

    pub fn is_pinned(&self) -> bool {
        self.kids.is_some()
    }

    fn map(&mut self) -> &mut BTreeMap<String, KeyInfo> {
        self.kids.get_or_insert_with(BTreeMap::new)
    }

    /// Add raw keys (64 hex each).
    pub fn add_hex<S: AsRef<str>>(&mut self, keys: &[S]) -> Result<(), KeyError> {
        for k in keys {
            let kid = hex32(k.as_ref())?;
            self.map().entry(kid).or_insert(KeyInfo {
                status: "active".into(),
                scope: None,
                log_id: None,
            });
        }
        Ok(())
    }

    /// Add every key of a well-known keys document.
    pub fn add_well_known(&mut self, doc: &Value) -> Result<(), KeyError> {
        let keys = doc
            .get("keys")
            .and_then(Value::as_array)
            .ok_or_else(|| KeyError("keys document has no \"keys\" array".into()))?;
        for (i, k) in keys.iter().enumerate() {
            let kid = k
                .get("kid")
                .and_then(Value::as_str)
                .ok_or_else(|| KeyError(format!("keys[{i}] has no kid")))?;
            let kid = hex32(kid).map_err(|e| KeyError(format!("keys[{i}]: {e}")))?;
            if let Some(kty) = k.get("kty").and_then(Value::as_str)
                && kty != "OKP"
            {
                return Err(KeyError(format!("keys[{i}]: kty {kty:?} is not OKP")));
            }
            if let Some(crv) = k.get("crv").and_then(Value::as_str)
                && crv != "Ed25519"
            {
                return Err(KeyError(format!("keys[{i}]: crv {crv:?} is not Ed25519")));
            }
            if let Some(x) = k.get("x").and_then(Value::as_str) {
                let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD
                    .decode(x.trim_end_matches('='))
                    .map_err(|e| KeyError(format!("keys[{i}]: x is not base64url: {e}")))?;
                if hex::encode(raw) != kid {
                    return Err(KeyError(format!(
                        "keys[{i}]: x and kid name different keys"
                    )));
                }
            }
            let info = KeyInfo {
                status: k
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("active")
                    .to_owned(),
                scope: k.get("scope").and_then(Value::as_str).map(str::to_owned),
                log_id: k.get("log_id").and_then(Value::as_str).map(str::to_owned),
            };
            self.map().insert(kid, info);
        }
        Ok(())
    }

    /// Parse the forms the CLI and the wasm page accept in one string:
    /// empty ⇒ unpinned; a JSON object ⇒ a well-known document; a JSON
    /// array ⇒ hex keys; otherwise hex keys separated by commas or
    /// whitespace.
    pub fn parse(s: &str) -> Result<Self, KeyError> {
        let t = s.trim();
        let mut k = Self::unpinned();
        if t.is_empty() {
            return Ok(k);
        }
        if t.starts_with('{') || t.starts_with('[') {
            let v: Value = serde_json::from_str(t)
                .map_err(|e| KeyError(format!("keys are not valid JSON: {e}")))?;
            match &v {
                Value::Object(_) => k.add_well_known(&v)?,
                Value::Array(a) => {
                    let hexes: Vec<&str> = a
                        .iter()
                        .map(|x| {
                            x.as_str()
                                .ok_or_else(|| KeyError("key array holds a non-string".into()))
                        })
                        .collect::<Result<_, _>>()?;
                    k.add_hex(&hexes)?;
                }
                _ => return Err(KeyError("keys must be an object or an array".into())),
            }
            return Ok(k);
        }
        let hexes: Vec<&str> = t
            .split(|c: char| c == ',' || c.is_whitespace())
            .filter(|x| !x.is_empty())
            .collect();
        k.add_hex(&hexes)?;
        Ok(k)
    }

    /// Union with another set (pinned if either is).
    pub fn extend(&mut self, other: TrustedKeys) {
        if let Some(m) = other.kids {
            let mine = self.map();
            for (k, v) in m {
                mine.entry(k).or_insert(v);
            }
        }
    }

    pub fn status(&self, kid_hex: &str) -> KidStatus {
        match &self.kids {
            None => KidStatus::Unpinned,
            Some(m) => match m.get(&kid_hex.to_ascii_lowercase()) {
                Some(info) => KidStatus::Trusted {
                    retired: info.status == "retired",
                },
                None => KidStatus::Untrusted,
            },
        }
    }

    /// The set `brutor_evidence::bundle::VerifyOptions::trusted_kids` wants.
    pub fn kid_set(&self) -> Option<HashSet<String>> {
        self.kids.as_ref().map(|m| m.keys().cloned().collect())
    }

    pub fn kids(&self) -> Vec<String> {
        self.kids
            .as_ref()
            .map(|m| m.keys().cloned().collect())
            .unwrap_or_default()
    }

    pub fn info(&self, kid_hex: &str) -> Option<&KeyInfo> {
        self.kids.as_ref().and_then(|m| m.get(kid_hex))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn kid(seed: u8) -> String {
        hex::encode(
            ed25519_dalek::SigningKey::from_bytes(&[seed; 32])
                .verifying_key()
                .to_bytes(),
        )
    }

    #[test]
    fn well_known_document_is_parsed_and_checked() {
        let a = kid(1);
        let b = kid(2);
        let x = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(hex::decode(&a).unwrap());
        let doc = json!({"keys": [
            {"kid": a, "kty": "OKP", "crv": "Ed25519", "x": x, "status": "active"},
            {"kid": b, "status": "retired"}
        ]});
        let k = TrustedKeys::parse(&doc.to_string()).unwrap();
        assert_eq!(k.status(&a), KidStatus::Trusted { retired: false });
        assert_eq!(k.status(&b), KidStatus::Trusted { retired: true });
        assert_eq!(k.status(&kid(3)), KidStatus::Untrusted);
        // x naming another key is refused
        let bad = json!({"keys": [{"kid": b, "x": x}]});
        assert!(TrustedKeys::parse(&bad.to_string()).is_err());
    }

    #[test]
    fn hex_lists_and_unpinned() {
        assert_eq!(
            TrustedKeys::parse("").unwrap().status(&kid(1)),
            KidStatus::Unpinned
        );
        let k = TrustedKeys::parse(&format!("{}, {}", kid(1), kid(2).to_uppercase())).unwrap();
        assert_eq!(k.kids().len(), 2);
        assert!(TrustedKeys::parse("abcd").is_err());
        let k = TrustedKeys::parse(&json!([kid(4)]).to_string()).unwrap();
        assert!(k.is_pinned());
    }
}
