// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Canonical bytes and digests.
//!
//! `canonical_bytes` is RFC 8785 JCS, written here rather than delegated
//! to `serde_jcs`: that crate sorts object members by Unicode code point,
//! whereas RFC 8785 §3.2.3 sorts by UTF-16 code units, and the two orders
//! disagree when one key holds a character above U+FFFF and another one
//! in U+E000–U+FFFF (the upstream AAC corpus pins exactly this case). Two
//! digest-hygiene rules apply on top, because a canonicalisation that
//! silently accepts them produces identities nobody else can reproduce:
//!
//! * **No JSON floats anywhere.** ECMAScript number formatting differs
//!   across languages in the last digit; a record that contains `0.1`
//!   may hash differently in a verifier written in Go. Producers convert
//!   floats to exact decimal strings before sealing.
//! * **Integers within ±(2^53 − 1).** Anything larger is a string.
//!
//! `json_digest(v)` = SHA-256 over `canonical_bytes(v)`, the digest used
//! for every committed value (inputs, outputs, notice text) and for the
//! record identity itself.

use serde_json::Value;
use sha2::{Digest, Sha256};

pub const MAX_SAFE_INTEGER: u64 = (1u64 << 53) - 1;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum CanonError {
    #[error("float at {0} — digest-bearing JSON must not contain floats")]
    Float(String),
    #[error("integer at {0} outside ±(2^53−1)")]
    UnsafeInteger(String),
    /// A canonicalisation failure outside the two hygiene rules (used by
    /// callers that canonicalise derived structures, e.g. bundles).
    #[error("canonicalisation failed: {0}")]
    Jcs(String),
}

/// How numbers are written.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Numbers {
    /// Digest hygiene (records, bundles): floats and unsafe integers refused.
    Strict,
    /// RFC 8785 §3.2.2.3 as specified: floats in ECMAScript
    /// `Number.prototype.toString` form, integers as their decimal digits.
    EcmaScript,
}

fn write_value(
    v: &Value,
    out: &mut Vec<u8>,
    path: &mut String,
    mode: Numbers,
) -> Result<(), CanonError> {
    match v {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(b) => out.extend_from_slice(if *b { b"true" } else { b"false" }),
        Value::Number(n) if mode == Numbers::EcmaScript => {
            if let Some(f) = n.as_f64().filter(|_| n.is_f64()) {
                let s = es_number(f)
                    .ok_or_else(|| CanonError::Jcs(format!("non-finite number at {path}")))?;
                out.extend_from_slice(s.as_bytes());
            } else {
                // i64 / u64: the digits, as `serde_jcs` (and every signer of
                // an A2A card so far) wrote them.
                out.extend_from_slice(n.to_string().as_bytes());
            }
        }
        Value::Number(n) => {
            if n.is_f64() {
                return Err(CanonError::Float(path.clone()));
            }
            let safe = n
                .as_i64()
                .map(|i| i.unsigned_abs() <= MAX_SAFE_INTEGER)
                .or_else(|| n.as_u64().map(|u| u <= MAX_SAFE_INTEGER))
                .unwrap_or(false);
            if !safe {
                return Err(CanonError::UnsafeInteger(path.clone()));
            }
            // A safe integer's ECMAScript form is its plain decimal.
            out.extend_from_slice(n.to_string().as_bytes());
        }
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                let len = path.len();
                path.push_str(&format!("[{i}]"));
                write_value(item, out, path, mode)?;
                path.truncate(len);
            }
            out.push(b']');
        }
        Value::Object(map) => {
            // RFC 8785 §3.2.3: order by the UTF-16 code units of the names.
            let mut keys: Vec<(Vec<u16>, &String)> = map
                .keys()
                .map(|k| (k.encode_utf16().collect(), k))
                .collect();
            keys.sort_by(|a, b| a.0.cmp(&b.0));
            out.push(b'{');
            for (i, (_, k)) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_string(k, out);
                out.push(b':');
                let len = path.len();
                path.push('.');
                path.push_str(k);
                write_value(&map[k.as_str()], out, path, mode)?;
                path.truncate(len);
            }
            out.push(b'}');
        }
    }
    Ok(())
}

/// RFC 8785 §3.2.2.2 — the ECMAScript `JSON.stringify` string form: the
/// two mandatory escapes, the five short escapes, `\u00xx` for the rest
/// of C0, everything else (including `/` and non-ASCII) literal UTF-8.
fn write_string(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for ch in s.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\u{08}' => out.extend_from_slice(b"\\b"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\u{0c}' => out.extend_from_slice(b"\\f"),
            '\r' => out.extend_from_slice(b"\\r"),
            c if (c as u32) < 0x20 => {
                out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes())
            }
            c => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
}

/// RFC 8785 bytes of `v`. Floats and integers outside ±(2^53−1) are
/// refused with the JSON path of the first offender.
pub fn canonical_bytes(v: &Value) -> Result<Vec<u8>, CanonError> {
    let mut out = Vec::with_capacity(256);
    let mut path = String::from("$");
    write_value(v, &mut out, &mut path, Numbers::Strict)?;
    Ok(out)
}

/// RFC 8785 bytes of `v` with numbers written as its section 3.2.2.3 says (ECMAScript
/// `Number.prototype.toString`) instead of refused. For documents that
/// are canonicalised for a signature over someone else's JSON — A2A Agent
/// Cards may carry floats in extension params — where refusing a float is
/// not an option. Object members are ordered by UTF-16 code units, like
/// [`canonical_bytes`]. Never use it for a digest-bearing record: the
/// hygiene rules of [`canonical_bytes`] exist because float formatting is
/// where independent implementations disagree.
pub fn canonical_bytes_ecmascript(v: &Value) -> Result<Vec<u8>, CanonError> {
    let mut out = Vec::with_capacity(256);
    let mut path = String::from("$");
    write_value(v, &mut out, &mut path, Numbers::EcmaScript)?;
    Ok(out)
}

pub fn json_digest(v: &Value) -> Result<[u8; 32], CanonError> {
    Ok(Sha256::digest(canonical_bytes(v)?).into())
}

pub fn json_digest_hex(v: &Value) -> Result<String, CanonError> {
    json_digest(v).map(hex::encode)
}

/// Salted digest of a committed value (`salt_digests`):
/// `SHA-256(JCS(value) ‖ "|" ‖ salt)`, where `salt` is the lowercase-hex
/// string carried in `attestation.digest_salt` and is appended as its
/// ASCII bytes. A per-record random salt defeats dictionary recovery of
/// low-entropy values (an order id, a yes/no) from a published digest; a
/// holder of the original value and the record recomputes it exactly.
pub fn salted_digest_hex(v: &Value, salt_hex: &str) -> Result<String, CanonError> {
    let mut bytes = canonical_bytes(v)?;
    bytes.push(b'|');
    bytes.extend_from_slice(salt_hex.as_bytes());
    Ok(hex::encode(Sha256::digest(&bytes)))
}

/// The JSON value a captured body commits to: the parsed document when the
/// body is JSON (numbers canonicalised, see [`canonicalize_numbers`]), else
/// the body as a JSON string. Returns `(value, numbers_canonicalized)`.
pub fn body_value(body: &str) -> (Value, bool) {
    match serde_json::from_str::<Value>(body) {
        Ok(v) => canonicalize_numbers(&v),
        Err(_) => (Value::String(body.to_owned()), false),
    }
}

/// Convert any floats in `v` to exact decimal strings (and unsafe
/// integers to strings) so a value observed on the wire can be digested.
/// Records `numbers_canonicalized: true` when anything changed.
pub fn canonicalize_numbers(v: &Value) -> (Value, bool) {
    fn walk(v: &Value, changed: &mut bool) -> Value {
        match v {
            Value::Number(n) => {
                if n.is_f64() {
                    *changed = true;
                    let f = n.as_f64().unwrap_or(0.0);
                    Value::String(format_f64(f))
                } else if n
                    .as_i64()
                    .map(|i| i.unsigned_abs() > MAX_SAFE_INTEGER)
                    .unwrap_or(true)
                    && n.as_u64().map(|u| u > MAX_SAFE_INTEGER).unwrap_or(true)
                {
                    *changed = true;
                    Value::String(n.to_string())
                } else {
                    v.clone()
                }
            }
            Value::Array(items) => Value::Array(items.iter().map(|i| walk(i, changed)).collect()),
            Value::Object(map) => Value::Object(
                map.iter()
                    .map(|(k, i)| (k.clone(), walk(i, changed)))
                    .collect(),
            ),
            _ => v.clone(),
        }
    }
    let mut changed = false;
    let out = walk(v, &mut changed);
    (out, changed)
}

/// Shortest round-trip decimal for a float (what `serde_json` prints),
/// without an exponent for the common range so `1240.19` stays `"1240.19"`.
fn format_f64(f: f64) -> String {
    if f.is_finite() && f.fract() == 0.0 && f.abs() < 1e15 {
        format!("{}", f as i64)
    } else {
        let s = format!("{f}");
        s
    }
}

/// ECMAScript `Number::toString(x)` (ECMA-262, "Number::toString"), which RFC 8785
/// (section 3.2.2.3) adopts for JSON numbers. `None` for NaN / ±Infinity, which
/// JSON cannot carry.
///
/// The digits `s` (and their count `k`) come from Ryu: the shortest digit
/// string that round-trips, the one closest to the exact value, an exact
/// tie resolved to the even digit — which is precisely how ECMAScript
/// chooses `s`. (Rust's own `{:e}` is shortest too but rounds a tie up:
/// `0x43143ff3c1cb0959` is exactly …206.25 and must print `…206.2`.)
/// ECMAScript's placement rules then apply, with `n` the decimal exponent
/// such that the value is `0.s × 10^n`:
///
/// * `k ≤ n ≤ 21` — the digits followed by `n − k` zeros;
/// * `0 < n ≤ 21` — the first `n` digits, `.`, the rest;
/// * `−6 < n ≤ 0` — `0.`, `−n` zeros, the digits;
/// * otherwise exponent form `d[.ddd]e±(n−1)`.
///
/// `-0` is `0`.
pub fn es_number(x: f64) -> Option<String> {
    if !x.is_finite() {
        return None;
    }
    if x == 0.0 {
        return Some("0".to_owned());
    }
    let mut buf = ryu::Buffer::new();
    let repr = buf.format_finite(x.abs());
    let (mantissa, exp) = match repr.split_once(['e', 'E']) {
        Some((m, e)) => (m, e.parse::<i32>().ok()?),
        None => (repr, 0),
    };
    let (int_part, frac_part) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all = format!("{int_part}{frac_part}");
    // The decimal point sits after `point` characters of `all`.
    let mut point = int_part.len() as i32 + exp;
    let lead = all.len() - all.trim_start_matches('0').len();
    point -= lead as i32;
    let digits = all.trim_start_matches('0').trim_end_matches('0');
    if digits.is_empty() {
        return Some("0".to_owned());
    }
    let k = digits.len() as i32;
    let n = point;
    let mut s = String::with_capacity(k as usize + 8);
    if x < 0.0 {
        s.push('-');
    }
    if k <= n && n <= 21 {
        s.push_str(digits);
        s.extend(std::iter::repeat_n('0', (n - k) as usize));
    } else if 0 < n && n <= 21 {
        s.push_str(&digits[..n as usize]);
        s.push('.');
        s.push_str(&digits[n as usize..]);
    } else if -6 < n && n <= 0 {
        s.push_str("0.");
        s.extend(std::iter::repeat_n('0', (-n) as usize));
        s.push_str(digits);
    } else {
        let e = n - 1;
        s.push_str(&digits[..1]);
        if k > 1 {
            s.push('.');
            s.push_str(&digits[1..]);
        }
        s.push('e');
        s.push(if e < 0 { '-' } else { '+' });
        s.push_str(&e.unsigned_abs().to_string());
    }
    Some(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn jcs_sorts_by_utf16_and_escapes_minimally() {
        // RFC 8785 §3.2.3: "€" (€) sorts before "𐀀" (𐀀) by UTF-16 units,
        // whereas byte order would put the supplementary character first.
        let v = json!({"𐀀": 1, "€": 2, "b": 3, "a\n": 4});
        let s = String::from_utf8(canonical_bytes(&v).unwrap()).unwrap();
        assert_eq!(s, "{\"a\\n\":4,\"b\":3,\"€\":2,\"𐀀\":1}");
    }

    #[test]
    fn jcs_orders_by_utf16_units_not_code_points() {
        // U+FFFD vs U+10000: by code point U+FFFD sorts first; by UTF-16
        // units U+10000 is the surrogate pair D800 DC00 and sorts first.
        // This is the case `serde_jcs` gets wrong (upstream AAC vector
        // `canonical-key-sort-utf16-vs-codepoint`).
        let v = json!({"\u{FFFD}": 1, "\u{10000}": 2});
        let s = String::from_utf8(canonical_bytes(&v).unwrap()).unwrap();
        assert_eq!(s, "{\"\u{10000}\":2,\"\u{FFFD}\":1}");
    }

    #[test]
    fn jcs_escapes_match_ecmascript() {
        let v = json!({"s": "\u{1}\u{8}\t\n\u{c}\r\"\\/é\u{7f}"});
        let s = String::from_utf8(canonical_bytes(&v).unwrap()).unwrap();
        assert_eq!(s, "{\"s\":\"\\u0001\\b\\t\\n\\f\\r\\\"\\\\/é\u{7f}\"}");
        assert_eq!(
            String::from_utf8(canonical_bytes(&json!([null, true, false, 0, -7, {}])).unwrap())
                .unwrap(),
            "[null,true,false,0,-7,{}]"
        );
    }

    #[test]
    fn floats_and_unsafe_integers_rejected_with_path() {
        assert_eq!(
            canonical_bytes(&json!({"a": {"b": [1, 2.5]}})).unwrap_err(),
            CanonError::Float("$.a.b[1]".into())
        );
        assert_eq!(
            canonical_bytes(&json!({"n": 9007199254740992u64})).unwrap_err(),
            CanonError::UnsafeInteger("$.n".into())
        );
        assert!(
            canonical_bytes(&json!({"n": 9007199254740991u64, "m": -9007199254740991i64})).is_ok()
        );
    }

    #[test]
    fn number_canonicalisation_makes_values_digestable() {
        let (v, changed) = canonicalize_numbers(
            &json!({"total": 1240.19, "qty": 3, "big": 18446744073709551615u64}),
        );
        assert!(changed);
        assert_eq!(
            v,
            json!({"total": "1240.19", "qty": 3, "big": "18446744073709551615"})
        );
        assert!(json_digest(&v).is_ok());
        let (u, changed) = canonicalize_numbers(&json!({"qty": 3}));
        assert!(!changed);
        assert_eq!(u, json!({"qty": 3}));
    }

    #[test]
    fn salted_digest_differs_from_plain_and_is_deterministic_given_the_salt() {
        let v = json!({"order_id": "PO-1", "total": "12.50"});
        let salt = "0f".repeat(16);
        let a = salted_digest_hex(&v, &salt).unwrap();
        assert_eq!(a, salted_digest_hex(&v, &salt).unwrap());
        assert_ne!(a, json_digest_hex(&v).unwrap());
        assert_ne!(a, salted_digest_hex(&v, &"1e".repeat(16)).unwrap());
        // the preimage is exactly JCS ‖ "|" ‖ salt
        let mut pre = canonical_bytes(&v).unwrap();
        pre.push(b'|');
        pre.extend_from_slice(salt.as_bytes());
        assert_eq!(a, hex::encode(Sha256::digest(&pre)));
    }

    #[test]
    fn body_value_parses_json_and_wraps_text() {
        assert_eq!(body_value(r#"{"a":1.5}"#), (json!({"a": "1.5"}), true));
        assert_eq!(body_value(r#"{"a":1}"#), (json!({"a": 1}), false));
        assert_eq!(body_value("plain text"), (json!("plain text"), false));
    }

    /// RFC 8785 Appendix B — IEEE 754 bit patterns and their canonical
    /// ECMAScript form.
    #[test]
    fn es_number_matches_rfc8785_appendix_b() {
        let vectors: &[(u64, &str)] = &[
            (0x0000000000000000, "0"),
            (0x8000000000000000, "0"),
            (0x0000000000000001, "5e-324"),
            (0x8000000000000001, "-5e-324"),
            (0x7fefffffffffffff, "1.7976931348623157e+308"),
            (0xffefffffffffffff, "-1.7976931348623157e+308"),
            (0x4340000000000000, "9007199254740992"),
            (0xc340000000000000, "-9007199254740992"),
            (0x4430000000000000, "295147905179352830000"),
            (0x44b52d02c7e14af5, "9.999999999999997e+22"),
            (0x44b52d02c7e14af6, "1e+23"),
            (0x44b52d02c7e14af7, "1.0000000000000001e+23"),
            (0x444b1ae4d6e2ef4e, "999999999999999700000"),
            (0x444b1ae4d6e2ef4f, "999999999999999900000"),
            (0x444b1ae4d6e2ef50, "1e+21"),
            (0x3eb0c6f7a0b5ed8c, "9.999999999999997e-7"),
            (0x3eb0c6f7a0b5ed8d, "0.000001"),
            (0x41b3de4355555553, "333333333.3333332"),
            (0x41b3de4355555554, "333333333.33333325"),
            (0x41b3de4355555555, "333333333.3333333"),
            (0x41b3de4355555556, "333333333.3333334"),
            (0x41b3de4355555557, "333333333.33333343"),
            (0xbecbf647612f3696, "-0.0000033333333333333333"),
            (0x43143ff3c1cb0959, "1424953923781206.2"),
        ];
        for (bits, want) in vectors {
            assert_eq!(
                es_number(f64::from_bits(*bits)).as_deref(),
                Some(*want),
                "{bits:#018x}"
            );
        }
        assert_eq!(es_number(f64::NAN), None);
        assert_eq!(es_number(f64::INFINITY), None);
        assert_eq!(es_number(f64::NEG_INFINITY), None);
    }

    #[test]
    fn es_number_boundaries() {
        for (x, want) in [
            (1e21, "1e+21"),
            (1e20, "100000000000000000000"),
            (1e-7, "1e-7"),
            (1e-6, "0.000001"),
            (1.5e-7, "1.5e-7"),
            (0.1, "0.1"),
            (1.5, "1.5"),
            (-0.0, "0"),
            (100.0, "100"),
            (123e-20, "1.23e-18"),
            (2.5e-5, "0.000025"),
            (-3.75e30, "-3.75e+30"),
        ] {
            assert_eq!(es_number(x).as_deref(), Some(want), "{x:e}");
        }
    }

    #[test]
    fn ecmascript_mode_writes_numbers_and_orders_by_utf16() {
        let v = json!({"\u{FFFD}": 1.5, "\u{10000}": [1e21, -0.0, 7, 18446744073709551615u64], "a": 1e-7});
        let s = String::from_utf8(canonical_bytes_ecmascript(&v).unwrap()).unwrap();
        assert_eq!(
            s,
            "{\"a\":1e-7,\"\u{10000}\":[1e+21,0,7,18446744073709551615],\"\u{FFFD}\":1.5}"
        );
        // Strict mode still refuses the same document.
        assert!(canonical_bytes(&v).is_err());
    }

    #[test]
    fn digest_is_stable_across_key_order() {
        let a = json!({"x": 1, "y": {"b": 2, "a": 1}});
        let b = json!({"y": {"a": 1, "b": 2}, "x": 1});
        assert_eq!(json_digest_hex(&a).unwrap(), json_digest_hex(&b).unwrap());
    }
}
