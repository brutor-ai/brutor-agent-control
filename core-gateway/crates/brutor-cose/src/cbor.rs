// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Thin CBOR helpers over `ciborium`.
//!
//! Two things a COSE implementation must get right that a generic CBOR
//! library does not do for you:
//!
//! * **Canonical map ordering** for anything we sign or hash (RFC 8949
//!   §4.2.1: keys sorted by their encoded bytes, shorter first). Our
//!   protected headers and tree-head payloads go through
//!   [`canonical_map`]; ciborium then emits definite lengths and minimal
//!   integers, which is the rest of the core-deterministic profile.
//! * **Duplicate-key rejection**. `serde`-style map decoding silently
//!   keeps one of two duplicate keys; the scitt-cose and CPB reference
//!   implementations both treat that as the hazard it is. We decode to
//!   `Value` (which preserves pairs) and check before interpreting.
//!
//! Decoding is deliberately **order-tolerant**: third-party receipts
//! (DataTrails, Microsoft CCF) are bytes we cannot re-encode, and a
//! verifier that rejects a valid signature over a reordered map is
//! wrong, not strict.

use crate::CoseError;
pub use ciborium::value::{Integer, Value};

pub fn encode(v: &Value) -> Result<Vec<u8>, CoseError> {
    let mut out = Vec::with_capacity(256);
    ciborium::ser::into_writer(v, &mut out).map_err(|e| CoseError::Encode(e.to_string()))?;
    Ok(out)
}

pub fn decode(bytes: &[u8]) -> Result<Value, CoseError> {
    ciborium::de::from_reader(bytes).map_err(|e| CoseError::Decode(e.to_string()))
}

pub fn int(i: i64) -> Value {
    Value::Integer(Integer::from(i))
}

pub fn text(s: &str) -> Value {
    Value::Text(s.to_owned())
}

pub fn bytes(b: &[u8]) -> Value {
    Value::Bytes(b.to_vec())
}

/// Build a map whose entries are sorted per RFC 8949 §4.2.1
/// (length-first, then lexicographic over the encoded key bytes).
pub fn canonical_map(mut entries: Vec<(Value, Value)>) -> Result<Value, CoseError> {
    let mut keyed: Vec<(Vec<u8>, (Value, Value))> = Vec::with_capacity(entries.len());
    for e in entries.drain(..) {
        let k = encode(&e.0)?;
        keyed.push((k, e));
    }
    keyed.sort_by(|a, b| a.0.len().cmp(&b.0.len()).then_with(|| a.0.cmp(&b.0)));
    for w in keyed.windows(2) {
        if w[0].0 == w[1].0 {
            return Err(CoseError::DuplicateKey("canonical_map"));
        }
    }
    Ok(Value::Map(keyed.into_iter().map(|(_, e)| e).collect()))
}

/// Reject any map, at any depth, with two equal keys.
pub fn check_unique_keys(v: &Value, what: &'static str) -> Result<(), CoseError> {
    match v {
        Value::Map(entries) => {
            let mut seen: Vec<Vec<u8>> = Vec::with_capacity(entries.len());
            for (k, val) in entries {
                let enc = encode(k)?;
                if seen.contains(&enc) {
                    return Err(CoseError::DuplicateKey(what));
                }
                seen.push(enc);
                check_unique_keys(val, what)?;
            }
            Ok(())
        }
        Value::Array(items) => items.iter().try_for_each(|i| check_unique_keys(i, what)),
        Value::Tag(_, inner) => check_unique_keys(inner, what),
        _ => Ok(()),
    }
}

pub fn as_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Integer(i) => i64::try_from(*i).ok(),
        _ => None,
    }
}

pub fn as_u64(v: &Value) -> Option<u64> {
    match v {
        Value::Integer(i) => u64::try_from(*i).ok(),
        _ => None,
    }
}

pub fn as_bytes(v: &Value) -> Option<&[u8]> {
    match v {
        Value::Bytes(b) => Some(b.as_slice()),
        _ => None,
    }
}

pub fn as_text(v: &Value) -> Option<&str> {
    match v {
        Value::Text(t) => Some(t.as_str()),
        _ => None,
    }
}

/// Look up an integer-labelled entry in a map value.
pub fn map_get(map: &Value, label: i64) -> Option<&Value> {
    match map {
        Value::Map(entries) => entries
            .iter()
            .find(|(k, _)| as_i64(k) == Some(label))
            .map(|(_, v)| v),
        _ => None,
    }
}

/// Look up a text-labelled entry in a map value.
pub fn map_get_text<'a>(map: &'a Value, label: &str) -> Option<&'a Value> {
    match map {
        Value::Map(entries) => entries
            .iter()
            .find(|(k, _)| as_text(k) == Some(label))
            .map(|(_, v)| v),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_map_sorts_length_first_then_bytes() {
        let m = canonical_map(vec![
            (int(258), int(-16)),
            (int(1), int(-8)),
            (int(15), Value::Map(vec![])),
            (int(4), bytes(&[0u8; 32])),
        ])
        .unwrap();
        let keys: Vec<i64> = match m {
            Value::Map(e) => e.iter().map(|(k, _)| as_i64(k).unwrap()).collect(),
            _ => panic!(),
        };
        // 1, 4, 15 are one byte each (ordered by value); 258 needs two.
        assert_eq!(keys, vec![1, 4, 15, 258]);
    }

    #[test]
    fn canonical_map_rejects_duplicates() {
        assert!(matches!(
            canonical_map(vec![(int(1), int(1)), (int(1), int(2))]),
            Err(CoseError::DuplicateKey(_))
        ));
    }

    #[test]
    fn duplicate_keys_detected_when_decoding() {
        // a2 01 01 01 02 : {1: 1, 1: 2}
        let raw = [0xa2, 0x01, 0x01, 0x01, 0x02];
        let v = decode(&raw).unwrap();
        assert!(check_unique_keys(&v, "test").is_err());
    }
}
