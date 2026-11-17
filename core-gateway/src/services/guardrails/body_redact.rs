// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Body-rewrite helpers for output / input guardrail redaction.
//!
//! Every surface we run guardrails on has the same structural
//! problem: the detector returns a flat redacted string, but the
//! request / response body is a JSON document with text leaves
//! buried at arbitrary paths. We can't just substitute the body
//! with the redacted string (it'd no longer be valid JSON), so we
//! need to walk the original body, collect each string leaf with
//! its path, concatenate them for the detector, then splice the
//! redacted halves back into the original paths.
//!
//! This module hosts the shared walk + splice. Each surface picks
//! the subtree to scan (e.g. `params.arguments` for MCP tool
//! calls, `messages[*].content` for LLM chat, the entire body for
//! Skill inputs) and feeds it through here.
//!
//! Concatenation separator: a single `\n` between leaves. The
//! built-in detectors don't add or drop newlines so the split-
//! back count almost always matches; when it doesn't, the caller
//! falls back to the "log + forward unchanged" contract that
//! predated this helper, so a detector-side surprise can't
//! corrupt a live request.

use serde_json::Value;

/// One segment of a JSON path. `Index` is for array elements,
/// `Key` for object fields.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JsonSegment {
    Key(String),
    Index(usize),
}

/// Full path from the subtree root to one string leaf.
pub type JsonPath = Vec<JsonSegment>;

/// Pair of (path, original_string) collected during the walk.
#[derive(Clone, Debug)]
pub struct StringLeaf {
    pub path: JsonPath,
    pub value: String,
}

/// Walk `v` recursively, pushing every string leaf to `out` with
/// its full path relative to `v`. Numeric / boolean / null leaves
/// are skipped — there's nothing to redact in them, and they
/// shouldn't appear in detector input either.
pub fn collect_string_leaves(v: &Value, out: &mut Vec<StringLeaf>) {
    fn walk(v: &Value, path: &mut JsonPath, out: &mut Vec<StringLeaf>) {
        match v {
            Value::String(s) => out.push(StringLeaf {
                path: path.clone(),
                value: s.clone(),
            }),
            Value::Object(map) => {
                for (k, vv) in map {
                    path.push(JsonSegment::Key(k.clone()));
                    walk(vv, path, out);
                    path.pop();
                }
            }
            Value::Array(arr) => {
                for (i, vv) in arr.iter().enumerate() {
                    path.push(JsonSegment::Index(i));
                    walk(vv, path, out);
                    path.pop();
                }
            }
            _ => {}
        }
    }
    let mut path = Vec::new();
    walk(v, &mut path, out);
}

/// Splice `new_value` into `v` at `path`. Silently returns false
/// (no mutation) if the path doesn't resolve — that protects the
/// caller from a path that became stale because the body was
/// rewritten by an earlier splice.
pub fn set_string_at_path(v: &mut Value, path: &JsonPath, new_value: &str) -> bool {
    if path.is_empty() {
        return false;
    }
    let mut cur: &mut Value = v;
    for seg in &path[..path.len() - 1] {
        let next = match seg {
            JsonSegment::Key(k) => cur.as_object_mut().and_then(|m| m.get_mut(k)),
            JsonSegment::Index(i) => cur.as_array_mut().and_then(|a| a.get_mut(*i)),
        };
        match next {
            Some(n) => cur = n,
            None => return false,
        }
    }
    match path.last().unwrap() {
        JsonSegment::Key(k) => {
            if let Some(m) = cur.as_object_mut() {
                m.insert(k.clone(), Value::String(new_value.to_string()));
                return true;
            }
        }
        JsonSegment::Index(i) => {
            if let Some(a) = cur.as_array_mut() {
                if let Some(slot) = a.get_mut(*i) {
                    *slot = Value::String(new_value.to_string());
                    return true;
                }
            }
        }
    }
    false
}

/// Concatenate the leaves' values with `\n` so the detector sees
/// one input. The separator MUST be the same one `split_redacted`
/// uses on the way back.
pub const REDACT_SEPARATOR: &str = "\n";

pub fn join_for_detector(leaves: &[StringLeaf]) -> String {
    leaves
        .iter()
        .map(|l| l.value.as_str())
        .collect::<Vec<_>>()
        .join(REDACT_SEPARATOR)
}

/// Split the detector's redacted output back into per-leaf
/// strings. Returns `Some(parts)` only when the split count
/// matches the leaf count exactly — the caller must treat any
/// mismatch as "redaction failed, forward original" so the body
/// can't end up partially mutated.
pub fn split_redacted(redacted: &str, expected: usize) -> Option<Vec<&str>> {
    let parts: Vec<&str> = redacted.split(REDACT_SEPARATOR).collect();
    if parts.len() == expected {
        Some(parts)
    } else {
        None
    }
}

/// Convenience wrapper: walk a JSON body, find a subtree by name
/// at the top level, extract its string leaves, concat for the
/// detector, return the concatenated text + the leaves so the
/// caller can splice back later. Returns `None` if the body
/// doesn't parse or the subtree isn't present / has no string
/// leaves.
pub fn extract_subtree_for_check(
    body: &[u8],
    subtree_key: &str,
) -> Option<(String, Vec<StringLeaf>)> {
    let parsed: Value = serde_json::from_slice(body).ok()?;
    let subtree = parsed.get(subtree_key)?;
    let mut leaves = Vec::new();
    collect_string_leaves(subtree, &mut leaves);
    if leaves.is_empty() {
        return None;
    }
    let combined = join_for_detector(&leaves);
    Some((combined, leaves))
}

/// Splice redacted leaves back under a top-level subtree key and
/// re-serialise. Returns the new body bytes, or `None` if the
/// split count mismatches the leaf count or any path-splice
/// fails — in either case the caller forwards the original body.
pub fn apply_redactions_to_subtree(
    original_body: &[u8],
    subtree_key: &str,
    leaves: &[StringLeaf],
    redacted: &str,
) -> Option<Vec<u8>> {
    let parts = split_redacted(redacted, leaves.len())?;
    let mut parsed: Value = serde_json::from_slice(original_body).ok()?;
    let subtree = parsed.get_mut(subtree_key)?;
    for (leaf, part) in leaves.iter().zip(parts.iter()) {
        if !set_string_at_path(subtree, &leaf.path, part) {
            return None;
        }
    }
    serde_json::to_vec(&parsed).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn leaves_of(v: &Value) -> Vec<StringLeaf> {
        let mut out = Vec::new();
        collect_string_leaves(v, &mut out);
        out
    }

    #[test]
    fn collect_flat_string() {
        let v = json!("hello");
        let leaves = leaves_of(&v);
        assert_eq!(leaves.len(), 1);
        assert_eq!(leaves[0].value, "hello");
        assert!(leaves[0].path.is_empty());
    }

    #[test]
    fn collect_object_strings() {
        let v = json!({"a": "x", "b": "y", "c": 1});
        let leaves = leaves_of(&v);
        assert_eq!(leaves.len(), 2);
        let texts: Vec<&str> = leaves.iter().map(|l| l.value.as_str()).collect();
        assert!(texts.contains(&"x"));
        assert!(texts.contains(&"y"));
    }

    #[test]
    fn collect_nested_object_array() {
        let v = json!({
            "params": {
                "name": "run_query",
                "arguments": {
                    "queries": ["SELECT 1", "DROP TABLE"]
                }
            }
        });
        let mut leaves = Vec::new();
        collect_string_leaves(&v, &mut leaves);
        // 3 string leaves: name, queries[0], queries[1]
        assert_eq!(leaves.len(), 3);
        let texts: Vec<&str> = leaves.iter().map(|l| l.value.as_str()).collect();
        assert!(texts.contains(&"run_query"));
        assert!(texts.contains(&"SELECT 1"));
        assert!(texts.contains(&"DROP TABLE"));
    }

    #[test]
    fn set_path_overwrites_string() {
        let mut v = json!({"a": {"b": "before"}});
        let path = vec![JsonSegment::Key("a".into()), JsonSegment::Key("b".into())];
        assert!(set_string_at_path(&mut v, &path, "after"));
        assert_eq!(v["a"]["b"], "after");
    }

    #[test]
    fn set_path_handles_array_index() {
        let mut v = json!({"xs": ["one", "two", "three"]});
        let path = vec![JsonSegment::Key("xs".into()), JsonSegment::Index(1)];
        assert!(set_string_at_path(&mut v, &path, "TWO"));
        assert_eq!(v["xs"], json!(["one", "TWO", "three"]));
    }

    #[test]
    fn set_path_creates_missing_top_level_key() {
        // serde_json's insert on an object always succeeds; for
        // our use case that's the right behaviour because callers
        // always splice INTO paths they collected from the same
        // body. Documented here so the unit test mirrors reality.
        let mut v = json!({"a": 1});
        let path = vec![JsonSegment::Key("missing".into())];
        assert!(set_string_at_path(&mut v, &path, "x"));
        assert_eq!(v["missing"], "x");
    }

    #[test]
    fn set_path_returns_false_when_parent_missing() {
        let mut v = json!({"a": 1});
        let path = vec![
            JsonSegment::Key("absent_parent".into()),
            JsonSegment::Key("child".into()),
        ];
        assert!(!set_string_at_path(&mut v, &path, "x"));
    }

    #[test]
    fn set_path_returns_false_on_wrong_parent_type() {
        // Path expects parent to be an object, but it's a number.
        let mut v = json!({"a": 1});
        let path = vec![
            JsonSegment::Key("a".into()),
            JsonSegment::Key("child".into()),
        ];
        assert!(!set_string_at_path(&mut v, &path, "x"));
    }

    #[test]
    fn join_split_roundtrip_matches() {
        let leaves = vec![
            StringLeaf {
                path: vec![],
                value: "alpha".into(),
            },
            StringLeaf {
                path: vec![],
                value: "beta".into(),
            },
            StringLeaf {
                path: vec![],
                value: "gamma".into(),
            },
        ];
        let joined = join_for_detector(&leaves);
        let parts = split_redacted(&joined, leaves.len()).unwrap();
        assert_eq!(parts, vec!["alpha", "beta", "gamma"]);
    }

    #[test]
    fn split_count_mismatch_returns_none() {
        // Detector deleted a newline somehow.
        let merged = "alpha beta\ngamma";
        assert!(split_redacted(merged, 3).is_none());
    }

    #[test]
    fn extract_subtree_pulls_out_text() {
        let body = br#"{"jsonrpc":"2.0","method":"tools/call","params":{"name":"run_query","arguments":{"query":"SELECT 1"}}}"#;
        let (combined, leaves) = extract_subtree_for_check(body, "params").unwrap();
        // Two string leaves: name + arguments.query
        assert_eq!(leaves.len(), 2);
        assert!(combined.contains("SELECT 1"));
        assert!(combined.contains("run_query"));
    }

    #[test]
    fn extract_subtree_missing_returns_none() {
        let body = br#"{"jsonrpc":"2.0"}"#;
        assert!(extract_subtree_for_check(body, "params").is_none());
    }

    #[test]
    fn apply_redactions_round_trip() {
        let body = br#"{"params":{"name":"run_query","arguments":{"query":"SELECT 1"}}}"#;
        let (_combined, leaves) = extract_subtree_for_check(body, "params").unwrap();
        // Simulate the detector replacing each leaf with the same
        // string suffixed by [REDACTED] — same count, same order.
        let redacted = leaves
            .iter()
            .map(|l| format!("{}[REDACTED]", l.value))
            .collect::<Vec<_>>()
            .join(REDACT_SEPARATOR);
        let new_body = apply_redactions_to_subtree(body, "params", &leaves, &redacted).unwrap();
        let parsed: Value = serde_json::from_slice(&new_body).unwrap();
        assert_eq!(parsed["params"]["arguments"]["query"], "SELECT 1[REDACTED]");
        assert_eq!(parsed["params"]["name"], "run_query[REDACTED]");
    }

    #[test]
    fn apply_redactions_mismatch_returns_none() {
        let body = br#"{"params":{"name":"x","arguments":{"q":"y"}}}"#;
        let (_combined, leaves) = extract_subtree_for_check(body, "params").unwrap();
        // Wrong number of parts after split.
        let bad_redacted = "only-one-line";
        assert!(apply_redactions_to_subtree(body, "params", &leaves, bad_redacted).is_none());
    }
}
