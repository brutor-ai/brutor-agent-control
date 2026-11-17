// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! RFC 8785 — JSON Canonicalization Scheme (JCS).
//!
//! One JSON value, one byte string: object members sorted by the UTF-16 code
//! units of their names, no insignificant whitespace, strings escaped exactly
//! as ECMAScript `JSON.stringify` escapes them, numbers serialised as
//! ECMAScript `Number.prototype.toString` prints an IEEE-754 double.
//!
//! Used by the audit chain (canonical row format v4) to put the JSON
//! decision records — `guardrails_skipped`, `pre_action_state`,
//! `compliance_tags`, … — inside the row hash. The property that makes that
//! possible: the writer hashes the in-memory `serde_json::Value`, Postgres
//! stores it (`jsonb` re-orders keys and normalises numbers, `json` keeps the
//! text as sent), the verifier reads it back — and all three canonicalise to
//! the same bytes, because JCS depends only on the JSON *data model*, never on
//! how a particular serialiser laid the text out.
//!
//! The control plane's reference verifier (`app/services/audit_chain_canonical.py`)
//! is an independent standard-library implementation of the same RFC; both
//! repos pin the RFC 8785 test vectors and the same Brutor row vectors.

use serde_json::Value;

/// The JCS text of `v`.
///
/// Total: every `serde_json::Value` has a canonical form. The RFC's error
/// cases (NaN, ±Infinity, lone surrogates) cannot be represented in one —
/// `serde_json` refuses non-finite numbers and invalid UTF-16 escapes at
/// parse time, and this crate is built without `arbitrary_precision` (pinned
/// by `a_non_finite_number_cannot_reach_a_value` below).
pub fn canonicalize(v: &Value) -> String {
    let mut out = String::with_capacity(128);
    write_value(&mut out, v);
    out
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => match n.as_f64().and_then(es_number) {
            Some(s) => out.push_str(&s),
            // Unreachable without `arbitrary_precision` (every Number is a
            // finite i64, u64 or f64). Kept total so the audit writer can
            // never panic on a value: the serialiser's own text is still a
            // deterministic function of the stored number.
            None => out.push_str(&n.to_string()),
        },
        Value::String(s) => write_string(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            // RFC 8785 §3.2.3: sort by the UTF-16 code units of the names,
            // not by code point or UTF-8 bytes — they differ once a name
            // holds a character above U+FFFF next to one in U+E000..U+FFFF.
            let mut members: Vec<(&String, &Value)> = map.iter().collect();
            members.sort_by(|a, b| a.0.encode_utf16().cmp(b.0.encode_utf16()));
            out.push('{');
            for (i, (k, val)) in members.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, k);
                out.push(':');
                write_value(out, val);
            }
            out.push('}');
        }
    }
}

/// RFC 8785 §3.2.2.2 — ECMAScript `JSON.stringify` string escaping: `"` and
/// `\`, the five short control escapes, `\u00xx` (lower-case hex) for the
/// other C0 controls, everything else literal UTF-8.
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{09}' => out.push_str("\\t"),
            '\u{0A}' => out.push_str("\\n"),
            '\u{0C}' => out.push_str("\\f"),
            '\u{0D}' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// RFC 8785 §3.2.2.3 — an IEEE-754 double as ECMAScript
/// `Number.prototype.toString` prints it. `None` for NaN and ±Infinity,
/// which JCS rejects.
///
/// The digit count comes from Rust's shortest round-tripping `{:e}`
/// formatter, the digits themselves from exact fixed-precision formatting
/// (closest, ties to even — ECMAScript's tie rule); the ES layout rules then
/// place the decimal point or choose the exponent form.
pub fn es_number(f: f64) -> Option<String> {
    if !f.is_finite() {
        return None;
    }
    if f == 0.0 {
        // Both zeros print "0" (ES: `(-0).toString() === "0"`).
        return Some("0".to_string());
    }
    // Step 1: the LENGTH of the shortest round-tripping digit string.
    let shortest = format!("{:e}", f.abs());
    let k = shortest
        .split_once('e')?
        .0
        .chars()
        .filter(|c| *c != '.')
        .count();
    // Step 2: the k-digit string closest to the exact value, ties to even —
    // ES's rule when more than one k-digit string round-trips
    // (0x43143ff3c1cb0959 is exactly …206.25: ES prints …206.2, while the
    // shortest-digits formatter alone rounds the tie up to …206.3). Fixed
    // precision formatting is exact and rounds half to even.
    let sci = format!("{:.*e}", k - 1, f.abs());
    let (mantissa, exp) = sci.split_once('e')?;
    let exp: i32 = exp.parse().ok()?;
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let digits = digits.trim_end_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let k = digits.len() as i32;
    // ES notation: value = 0.d1d2…dk × 10^n
    let n = exp + 1;
    let mut s = String::with_capacity(32);
    if f < 0.0 {
        s.push('-');
    }
    if k <= n && n <= 21 {
        s.push_str(&digits);
        s.extend(std::iter::repeat_n('0', (n - k) as usize));
    } else if 0 < n && n <= 21 {
        s.push_str(&digits[..n as usize]);
        s.push('.');
        s.push_str(&digits[n as usize..]);
    } else if -6 < n && n <= 0 {
        s.push_str("0.");
        s.extend(std::iter::repeat_n('0', (-n) as usize));
        s.push_str(&digits);
    } else {
        let e = n - 1;
        s.push_str(&digits[..1]);
        if k > 1 {
            s.push('.');
            s.push_str(&digits[1..]);
        }
        s.push('e');
        s.push(if e < 0 { '-' } else { '+' });
        s.push_str(&e.abs().to_string());
    }
    Some(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// RFC 8785 Appendix B — IEEE-754 bit patterns and the text JCS
    /// requires. Mirrored in the control plane's
    /// `tests/test_audit_chain_v4.py::RFC8785_NUMBERS`.
    const RFC8785_NUMBERS: &[(u64, &str)] = &[
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

    #[test]
    fn rfc8785_number_vectors() {
        for (bits, want) in RFC8785_NUMBERS {
            let f = f64::from_bits(*bits);
            assert_eq!(es_number(f).as_deref(), Some(*want), "{bits:016x}");
        }
        // The RFC's error cases.
        for bits in [
            0x7fffffffffffffff_u64,
            0x7ff0000000000000,
            0xfff0000000000000,
        ] {
            assert_eq!(es_number(f64::from_bits(bits)), None, "{bits:016x}");
        }
    }

    #[test]
    fn es_number_layout_boundaries() {
        // Each branch of Number::toString's layout, at its edges.
        for (f, want) in [
            (1.0, "1"),
            (-1.5, "-1.5"),
            (100.0, "100"),
            (1e20, "100000000000000000000"),
            (1e21, "1e+21"),
            (123456.789, "123456.789"),
            (0.1, "0.1"),
            (0.000001, "0.000001"),
            (0.0000001, "1e-7"),
            (1.5e-7, "1.5e-7"),
            (4.5, "4.5"),
            (0.002, "0.002"),
            (1e30, "1e+30"),
            (1e-27, "1e-27"),
        ] {
            assert_eq!(es_number(f).as_deref(), Some(want), "{f}");
        }
    }

    #[test]
    fn rfc8785_main_example() {
        // RFC 8785 §3.2.2 / Appendix — the worked example, parsed from its
        // JSON text so the parser's number handling is in the loop too.
        let input = r#"{
          "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
          "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
          "literals": [null, true, false]
        }"#;
        let v: Value = serde_json::from_str(input).unwrap();
        assert_eq!(
            canonicalize(&v),
            "{\"literals\":[null,true,false],\"numbers\":[333333333.3333333,1e+30,4.5,0.002,1e-27],\
             \"string\":\"\u{20ac}$\\u000f\\nA'B\\\"\\\\\\\\\\\"/\"}"
        );
    }

    #[test]
    fn rfc8785_utf16_key_ordering() {
        // RFC 8785 §3.2.3: U+1F600 sorts BEFORE U+FB33 by UTF-16 code units
        // (0xD83D < 0xFB33) though it is after it by code point.
        let input = r#"{
          "\u20ac": "Euro Sign",
          "\r": "Carriage Return",
          "\ufb33": "Hebrew Letter Dalet With Dagesh",
          "1": "One",
          "\ud83d\ude00": "Emoji: Grinning Face",
          "\u0080": "Control",
          "\u00f6": "Latin Small Letter O With Diaeresis"
        }"#;
        let v: Value = serde_json::from_str(input).unwrap();
        assert_eq!(
            canonicalize(&v),
            "{\"\\r\":\"Carriage Return\",\"1\":\"One\",\"\u{80}\":\"Control\",\
             \"\u{f6}\":\"Latin Small Letter O With Diaeresis\",\"\u{20ac}\":\"Euro Sign\",\
             \"\u{1f600}\":\"Emoji: Grinning Face\",\"\u{fb33}\":\"Hebrew Letter Dalet With Dagesh\"}"
        );
    }

    #[test]
    fn string_escaping_follows_json_stringify() {
        let v = json!("\u{0}\u{1}\u{8}\t\n\u{b}\u{c}\r\u{1f}\u{7f}\u{2028}\"\\/é");
        assert_eq!(
            canonicalize(&v),
            "\"\\u0000\\u0001\\b\\t\\n\\u000b\\f\\r\\u001f\u{7f}\u{2028}\\\"\\\\/é\""
        );
    }

    #[test]
    fn nested_structures_and_literals() {
        let v = json!({"b": [1, {"z": null, "a": [true, false]}], "a": {}, "c": []});
        assert_eq!(
            canonicalize(&v),
            r#"{"a":{},"b":[1,{"a":[true,false],"z":null}],"c":[]}"#
        );
    }

    #[test]
    fn layout_and_number_spelling_do_not_matter() {
        // The property the audit chain relies on: the same data, however it
        // was laid out, canonicalises identically.
        let a: Value = serde_json::from_str(r#"{"kind":"llm_quota","used":1.50,"n":10}"#).unwrap();
        let b: Value =
            serde_json::from_str("{ \"n\" : 1e1 ,\n \"used\":1.5, \"kind\" :\"llm_quota\" }")
                .unwrap();
        assert_eq!(canonicalize(&a), canonicalize(&b));
        assert_eq!(
            canonicalize(&a),
            r#"{"kind":"llm_quota","n":10,"used":1.5}"#
        );
    }

    #[test]
    fn integers_beyond_2_pow_53_serialise_as_doubles() {
        // I-JSON / JCS: numbers are IEEE-754 doubles.
        let v: Value =
            serde_json::from_str("[9007199254740993, 18446744073709551615, -1]").unwrap();
        assert_eq!(
            canonicalize(&v),
            "[9007199254740992,18446744073709552000,-1]"
        );
    }

    #[test]
    fn a_non_finite_number_cannot_reach_a_value() {
        // Why `canonicalize` can be total: serde_json (built here without
        // `arbitrary_precision`) refuses numbers that overflow a double and
        // cannot hold NaN or infinity.
        assert!(serde_json::from_str::<Value>("1e400").is_err());
        assert!(serde_json::Number::from_f64(f64::NAN).is_none());
        assert!(serde_json::Number::from_f64(f64::INFINITY).is_none());
    }
}
