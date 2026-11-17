// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The ONE reading of a request body that may be a JSON-RPC message.
//!
//! Every MCP control (capability ACL, approval holds, agent authorisation,
//! argument policy, run controls, input guardrails) decides from the parsed
//! message, and the upstream executes the bytes it is sent. When those were
//! two readings, a body one of them could read and the other could not went
//! to the upstream ungoverned: the classifier skipped a UTF-8 BOM and said
//! "a message", the handler parsed the unstripped bytes, got nothing, ran no
//! control and forwarded the bytes, which an upstream that skips a BOM
//! (Python's `json.loads` on bytes) executes.
//!
//! [`read`] is that one reading. Its result is what the classifier answers
//! from, what every control reads, and (for a message) the bytes that are
//! forwarded:
//!
//! * [`RpcBody::Message`]: exactly one JSON object with `jsonrpc` or
//!   `method`, no key written twice at any depth. The value, and the bytes
//!   it was parsed from (the body without a BOM).
//! * [`RpcBody::Batch`]: a JSON array holding such an object. Refused.
//! * [`RpcBody::Unreadable`]: a body a JSON-RPC server might execute and
//!   this gateway cannot read as one message. Refused (400), never
//!   forwarded:
//!     - a message or batch with a key written twice (`{"name":"a",
//!       "name":"b"}`): this parser would judge the last, a first-wins
//!       upstream parser act on the first;
//!     - text that begins like JSON, names `"jsonrpc"` or `"method"`, and is
//!       not one JSON value (a syntax error a lenient parser forgives,
//!       trailing data, two concatenated messages);
//!     - JSON in UTF-16 or UTF-32, which some JSON parsers decode
//!       (Python's does) and this one does not.
//! * [`RpcBody::Other`]: anything else (an empty body, a form, JSON that is
//!   not JSON-RPC). Forwarded as plain HTTP, as before.

use std::collections::HashSet;
use std::fmt;

use serde::de::{DeserializeSeed, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};

/// What a request body is, for governance purposes.
#[derive(Debug, PartialEq)]
pub enum RpcBody<'a> {
    /// One JSON-RPC message and the bytes it was parsed from.
    Message { value: Value, bytes: &'a [u8] },
    /// A JSON-RPC batch.
    Batch,
    /// Might be executed as JSON-RPC and cannot be read as one message.
    Unreadable(&'static str),
    /// Not JSON-RPC; the parsed value when the body is JSON.
    Other(Option<Value>),
}

const UTF8_BOM: &[u8] = b"\xEF\xBB\xBF";

/// Read a request body once. See the module documentation.
pub fn read(body: &[u8]) -> RpcBody<'_> {
    if is_utf16_or_utf32_json(body) {
        return RpcBody::Unreadable("the body is not UTF-8");
    }
    let bytes = body.strip_prefix(UTF8_BOM).unwrap_or(body);
    let first = bytes.iter().find(|b| !b.is_ascii_whitespace());
    if !matches!(first, Some(b'{') | Some(b'[')) {
        return RpcBody::Other(None);
    }
    match parse_unique(bytes) {
        Ok(value) => match &value {
            Value::Object(_) if is_rpc(&value) => RpcBody::Message { value, bytes },
            Value::Array(items) if items.iter().any(is_rpc) => RpcBody::Batch,
            _ => RpcBody::Other(Some(value)),
        },
        Err(Unparsed::DuplicateKey) => match serde_json::from_slice::<Value>(bytes) {
            // Which of the two values a reader takes is the reader's
            // choice; nothing here can be judged.
            Ok(v) if is_rpc(&v) || v.as_array().is_some_and(|a| a.iter().any(is_rpc)) => {
                RpcBody::Unreadable("a JSON-RPC message must not name a key twice")
            }
            Ok(v) => RpcBody::Other(Some(v)),
            Err(_) => RpcBody::Other(None),
        },
        Err(Unparsed::Syntax) if names_json_rpc(bytes) => {
            RpcBody::Unreadable("the body is not one JSON value")
        }
        Err(Unparsed::Syntax) => RpcBody::Other(None),
    }
}

fn is_rpc(v: &Value) -> bool {
    v.get("jsonrpc").is_some() || v.get("method").is_some()
}

/// Whether text that is not one JSON value names the members that make a
/// JSON-RPC message.
fn names_json_rpc(bytes: &[u8]) -> bool {
    let has = |needle: &[u8]| bytes.windows(needle.len()).any(|w| w == needle);
    has(b"\"jsonrpc\"") || has(b"\"method\"")
}

/// JSON text in UTF-16 or UTF-32: a byte-order mark of one of them, or an
/// opening `{` / `[` with the NUL bytes of a wider encoding around it.
fn is_utf16_or_utf32_json(body: &[u8]) -> bool {
    if body.starts_with(b"\xFF\xFE") || body.starts_with(b"\xFE\xFF") {
        return true;
    }
    matches!(
        body,
        [b'{' | b'[', 0, ..] | [0, b'{' | b'[', ..] | [0, 0, 0, b'{' | b'[', ..]
    )
}

enum Unparsed {
    DuplicateKey,
    Syntax,
}

/// The message `parse_unique`'s visitor fails with for a repeated key.
const DUPLICATE: &str = "brutor: duplicate key";

/// Parse one JSON value, refusing an object that names a key twice.
fn parse_unique(bytes: &[u8]) -> Result<Value, Unparsed> {
    let mut de = serde_json::Deserializer::from_slice(bytes);
    let value = Unique.deserialize(&mut de).map_err(|e| {
        if e.to_string().starts_with(DUPLICATE) {
            Unparsed::DuplicateKey
        } else {
            Unparsed::Syntax
        }
    })?;
    de.end().map_err(|_| Unparsed::Syntax)?;
    Ok(value)
}

/// Builds a `serde_json::Value`, as `Value`'s own `Deserialize` does, and
/// fails on a repeated object key instead of keeping the last.
struct Unique;

impl<'de> DeserializeSeed<'de> for Unique {
    type Value = Value;
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> Result<Value, D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Unique {
    type Value = Value;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a JSON value")
    }
    fn visit_bool<E>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }
    fn visit_i64<E>(self, v: i64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }
    fn visit_u64<E>(self, v: u64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }
    fn visit_f64<E>(self, v: f64) -> Result<Value, E> {
        Ok(Number::from_f64(v).map_or(Value::Null, Value::Number))
    }
    fn visit_str<E>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_owned()))
    }
    fn visit_string<E>(self, v: String) -> Result<Value, E> {
        Ok(Value::String(v))
    }
    fn visit_unit<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut items = Vec::with_capacity(seq.size_hint().unwrap_or(0).min(1024));
        while let Some(item) = seq.next_element_seed(Unique)? {
            items.push(item);
        }
        Ok(Value::Array(items))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let mut seen: HashSet<String> = HashSet::new();
        let mut object = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if !seen.insert(key.clone()) {
                return Err(serde::de::Error::custom(DUPLICATE));
            }
            object.insert(key, map.next_value_seed(Unique)?);
        }
        Ok(Value::Object(object))
    }
}

/// JSON-RPC 2.0 error body for a body [`read`] calls unreadable.
pub fn unreadable_error(reason: &str) -> Value {
    serde_json::json!({
        "jsonrpc": "2.0",
        "id": null,
        "error": {
            "code": -32700,
            "message": format!("The gateway cannot read this request as one JSON-RPC message: {reason}"),
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const CALL: &[u8] = br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"t","arguments":{"n":1,"f":1.5,"big":18446744073709551615,"neg":-3,"s":"x","b":true,"z":null,"a":[1,{"k":"v"}]}}}"#;

    fn message(body: &[u8]) -> Option<(Value, Vec<u8>)> {
        match read(body) {
            RpcBody::Message { value, bytes } => Some((value, bytes.to_vec())),
            _ => None,
        }
    }

    #[test]
    fn the_value_is_what_serde_json_parses() {
        let (value, bytes) = message(CALL).expect("a message");
        assert_eq!(value, serde_json::from_slice::<Value>(CALL).unwrap());
        assert_eq!(bytes, CALL);
        assert!(message(br#"{"method":"tools/call"}"#).is_some());
        assert!(message(b"  \n\t{\"method\":\"x\"}  \n").is_some());
    }

    /// The value the controls read and the bytes that are forwarded
    /// are the same reading, with or without a BOM.
    #[test]
    fn a_bom_is_skipped_once_for_the_value_and_for_the_bytes() {
        let mut body = b"\xEF\xBB\xBF  ".to_vec();
        body.extend_from_slice(CALL);
        let (value, bytes) = message(&body).expect("a message");
        assert_eq!(value["params"]["name"], json!("t"));
        assert_eq!(bytes, &body[3..]);
        assert_eq!(serde_json::from_slice::<Value>(&bytes).unwrap(), value);
        assert_eq!(read(b"\xEF\xBB\xBF[{\"method\":\"x\"}]"), RpcBody::Batch);
    }

    #[test]
    fn batches_are_batches() {
        assert_eq!(
            read(br#"[{"jsonrpc":"2.0","id":1,"method":"tools/call"}]"#),
            RpcBody::Batch
        );
        assert_eq!(
            read(br#"[1, "x", {"method":"tools/call"}]"#),
            RpcBody::Batch
        );
    }

    #[test]
    fn what_is_not_json_rpc_is_other() {
        assert_eq!(read(b""), RpcBody::Other(None));
        assert_eq!(read(b"not json"), RpcBody::Other(None));
        assert_eq!(read(b"a=1&method=tools/call"), RpcBody::Other(None));
        assert_eq!(
            read(br#"{"query":"weather"}"#),
            RpcBody::Other(Some(json!({"query": "weather"})))
        );
        assert_eq!(read(b"[]"), RpcBody::Other(Some(json!([]))));
        assert_eq!(
            read(br#"[{"a":1},{"b":2}]"#),
            RpcBody::Other(Some(json!([{"a": 1}, {"b": 2}])))
        );
        // Begins like JSON, is not JSON, and names nothing of JSON-RPC.
        assert_eq!(read(b"[INFO] started"), RpcBody::Other(None));
        assert_eq!(read(b"{{ template }}"), RpcBody::Other(None));
        // JSON that is not JSON-RPC may repeat a key: nothing is judged.
        assert_eq!(
            read(br#"{"q":"a","q":"b"}"#),
            RpcBody::Other(Some(json!({"q": "b"})))
        );
    }

    #[test]
    fn a_key_written_twice_in_a_message_is_unreadable_at_any_depth() {
        for body in [
            &br#"{"jsonrpc":"2.0","method":"tools/list","method":"tools/call"}"#[..],
            br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"safe","name":"delete_customer"}}"#,
            br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"pay","arguments":{"amount":20000,"amount":1}}}"#,
            br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"pay","arguments":{"o":[{"a":1,"a":2}]}}}"#,
            br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"a"},"params":{"name":"b"}}"#,
            br#"[{"method":"tools/call","id":1,"id":2}]"#,
            // The escaped spelling of the same key.
            br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"a","name":"b"}}"#,
        ] {
            assert!(
                matches!(read(body), RpcBody::Unreadable(_)),
                "{}",
                String::from_utf8_lossy(body)
            );
        }
        // The same key in two different objects is not a repeat.
        assert!(message(br#"{"method":"m","params":{"a":{"x":1},"b":{"x":2}}}"#).is_some());
    }

    #[test]
    fn json_rpc_that_is_not_one_json_value_is_unreadable() {
        for body in [
            // Trailing data, two messages, a trailing comma, a comment,
            // single quotes, an unquoted key.
            &br#"{"jsonrpc":"2.0","method":"tools/call"} trailing"#[..],
            br#"{"jsonrpc":"2.0","method":"tools/list"}{"jsonrpc":"2.0","method":"tools/call"}"#,
            br#"{"jsonrpc":"2.0","method":"tools/call",}"#,
            b"{\"jsonrpc\":\"2.0\", /* c */ \"method\":\"tools/call\"}",
            br#"{"method":'tools/call'}"#,
            br#"{"method":"tools/call", params: {}}"#,
            br#"[{"method":"tools/call"}"#,
        ] {
            assert!(
                matches!(read(body), RpcBody::Unreadable(_)),
                "{}",
                String::from_utf8_lossy(body)
            );
        }
    }

    #[test]
    fn json_in_a_wider_encoding_is_unreadable() {
        let text = r#"{"jsonrpc":"2.0","method":"tools/call"}"#;
        let utf16le: Vec<u8> = text.encode_utf16().flat_map(u16::to_le_bytes).collect();
        let utf16be: Vec<u8> = text.encode_utf16().flat_map(u16::to_be_bytes).collect();
        let utf32le: Vec<u8> = text
            .chars()
            .flat_map(|c| (c as u32).to_le_bytes())
            .collect();
        let utf32be: Vec<u8> = text
            .chars()
            .flat_map(|c| (c as u32).to_be_bytes())
            .collect();
        let with = |bom: &[u8], rest: &[u8]| [bom, rest].concat();
        for body in [
            utf16le.clone(),
            utf16be.clone(),
            utf32le.clone(),
            utf32be,
            with(b"\xFF\xFE", &utf16le),
            with(b"\xFE\xFF", &utf16be),
            with(b"\xFF\xFE\x00\x00", &utf32le),
        ] {
            assert!(matches!(read(&body), RpcBody::Unreadable(_)), "{body:?}");
        }
    }

    #[test]
    fn deep_nesting_is_refused_not_overflowed() {
        let mut body = br#"{"method":"m","params":"#.to_vec();
        body.extend(std::iter::repeat(b'[').take(100_000));
        assert!(matches!(read(&body), RpcBody::Unreadable(_)));
    }
}
