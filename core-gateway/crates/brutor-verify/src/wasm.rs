// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `wasm-bindgen` exports for the static verifier page (`web/`).
//!
//! Every function takes strings and returns the [`Report`] as a JSON
//! string — the page never needs a second binding surface. `keys` accepts
//! what [`TrustedKeys::parse`] accepts: empty (unpinned), a well-known keys
//! document, a JSON array of hex keys, or hex keys separated by commas.

use crate::verify::input_error;
use crate::{BundleOptions, Report, TrustedKeys};
use base64::Engine as _;
use serde_json::Value;
use wasm_bindgen::prelude::*;

fn out(r: &Report) -> String {
    serde_json::to_string(r)
        .unwrap_or_else(|e| format!(r#"{{"ok":false,"exit_code":2,"error":"serialise: {e}"}}"#))
}

fn keys_or(artefact: &'static str, keys: &str) -> Result<TrustedKeys, String> {
    TrustedKeys::parse(keys).map_err(|e| out(&input_error(artefact, format!("keys: {e}"))))
}

fn b64(s: &str) -> Result<Vec<u8>, String> {
    let t: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    base64::engine::general_purpose::STANDARD
        .decode(t)
        .map_err(|e| e.to_string())
}

/// The crate version, for the page footer.
#[wasm_bindgen]
pub fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// Verify a bundle (or a bundle API response wrapping one).
#[wasm_bindgen]
pub fn verify_bundle_json(bundle: &str, keys: &str) -> String {
    verify_bundle_json_with(bundle, keys, "{}")
}

/// Like [`verify_bundle_json`], with `options` = `{"expected_digest"?,
/// "witness_keys"?: [hex], "tsa_pins"?: [pem|hex]}`.
#[wasm_bindgen]
pub fn verify_bundle_json_with(bundle: &str, keys: &str, options: &str) -> String {
    let keys = match keys_or("bundle", keys) {
        Ok(k) => k,
        Err(e) => return e,
    };
    let v: Value = match serde_json::from_str(bundle) {
        Ok(v) => v,
        Err(e) => return out(&input_error("bundle", format!("bundle is not JSON: {e}"))),
    };
    let o: Value = serde_json::from_str(if options.trim().is_empty() {
        "{}"
    } else {
        options
    })
    .unwrap_or(Value::Null);
    let list = |k: &str| -> Vec<String> {
        o.get(k)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default()
    };
    let (inner, wrapped_digest) = crate::unwrap_bundle(&v);
    let expected = o
        .get("expected_digest")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().to_ascii_lowercase())
        .or(wrapped_digest);
    out(&crate::verify_bundle(
        inner,
        &BundleOptions {
            keys,
            expected_digest: expected,
            witness_keys: list("witness_keys"),
            tsa_pins: list("tsa_pins"),
        },
    ))
}

/// Verify a record (JSON) and, when `statement_b64` is non-empty, its
/// Signed Statement.
#[wasm_bindgen]
pub fn verify_record_json(record: &str, statement_b64: &str, keys: &str) -> String {
    let keys = match keys_or("record", keys) {
        Ok(k) => k,
        Err(e) => return e,
    };
    let rec: Value = match serde_json::from_str(record) {
        Ok(v) => v,
        Err(e) => return out(&input_error("record", format!("record is not JSON: {e}"))),
    };
    let st = if statement_b64.trim().is_empty() {
        None
    } else {
        match b64(statement_b64) {
            Ok(raw) => Some(raw),
            Err(e) => {
                return out(&input_error(
                    "record",
                    format!("statement is not base64: {e}"),
                ));
            }
        }
    };
    out(&crate::verify_record(&rec, st.as_deref(), None, &keys))
}

/// Verify a Signed Statement (base64); `record` (JSON) is optional.
#[wasm_bindgen]
pub fn verify_statement_b64(statement_b64: &str, record: &str, keys: &str) -> String {
    let keys = match keys_or("statement", keys) {
        Ok(k) => k,
        Err(e) => return e,
    };
    let rec: Option<Value> = if record.trim().is_empty() {
        None
    } else {
        match serde_json::from_str(record) {
            Ok(v) => Some(v),
            Err(e) => {
                return out(&input_error(
                    "statement",
                    format!("record is not JSON: {e}"),
                ));
            }
        }
    };
    out(&crate::verify_statement_b64(
        statement_b64,
        rec.as_ref(),
        &keys,
    ))
}

/// Verify a tree head (base64) and, when `previous_b64` is non-empty, its
/// consistency with that earlier head (`consistency` = comma-separated hex
/// nodes for non-adjacent heads).
#[wasm_bindgen]
pub fn verify_head_b64(
    head_b64: &str,
    previous_b64: &str,
    consistency: &str,
    keys: &str,
) -> String {
    let keys = match keys_or("tree_head", keys) {
        Ok(k) => k,
        Err(e) => return e,
    };
    let Ok(raw) = b64(head_b64) else {
        return out(&input_error("tree_head", "head is not base64".into()));
    };
    let prev = if previous_b64.trim().is_empty() {
        None
    } else {
        match b64(previous_b64) {
            Ok(p) => Some(p),
            Err(_) => {
                return out(&input_error(
                    "tree_head",
                    "previous head is not base64".into(),
                ));
            }
        }
    };
    let nodes: Vec<String> = consistency
        .split(|c: char| c == ',' || c.is_whitespace())
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    out(&crate::verify_head(
        &raw,
        prev.as_deref(),
        if nodes.is_empty() { None } else { Some(&nodes) },
        &keys,
    ))
}

/// Grade a receipt (base64) over a head statement (base64). `kind` may be
/// empty (detected); `pin` is the witness key hex or TSA PEM/fingerprint.
#[wasm_bindgen]
pub fn verify_receipt_b64(receipt_b64: &str, statement_b64: &str, kind: &str, pin: &str) -> String {
    let (Ok(receipt), Ok(statement)) = (b64(receipt_b64), b64(statement_b64)) else {
        return out(&input_error(
            "receipt",
            "receipt and statement must be base64".into(),
        ));
    };
    let kind = Some(kind.trim()).filter(|k| !k.is_empty());
    let pin = Some(pin.trim()).filter(|p| !p.is_empty());
    out(&crate::verify_receipt(&receipt, &statement, kind, pin))
}
