// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Provenance of the tool an MCP call invoked.
//!
//! An audit row that names a tool but not *which definition* of it was in
//! force says less than it appears to: a tool's description or schema can
//! change under the same name (the rug-pull a registry verifies against), and
//! the server behind it can be upgraded without anyone telling the gateway.
//! Two facts close that gap on every `tools/call` row:
//!
//! * `server_version` — what the upstream announced in `initialize`
//!   (`result.serverInfo.version`), as this proxy instance last saw it.
//! * `tool_definition_hash` — SHA-256 over the canonical JSON of the tool's
//!   definition as the upstream last listed it through this proxy, falling
//!   back to the control plane's latest capability snapshot when no
//!   `tools/list` has passed through this instance yet.
//!
//! Both are witnessed by the gateway and covered by the row hash. Absence is
//! recorded as absence: a call to a tool the proxy never saw listed gets no
//! hash rather than a guessed one.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use moka::sync::Cache;
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::PgPool;

/// Tool name → definition hash, for one server.
type DefMap = Arc<HashMap<String, String>>;

/// Longest version string we will record; the column is VARCHAR(64).
const MAX_VERSION_LEN: usize = 64;

fn versions() -> &'static Cache<String, String> {
    static C: OnceLock<Cache<String, String>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(60 * 60))
            .build()
    })
}

fn definitions() -> &'static Cache<String, DefMap> {
    static C: OnceLock<Cache<String, DefMap>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(10 * 60))
            .build()
    })
}

/// Canonical JSON — object keys sorted, no whitespace — so the same
/// definition hashes the same whatever order a server emits its keys in.
pub fn canonical_json(v: &Value, out: &mut String) {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(k).unwrap_or_default());
                out.push(':');
                canonical_json(&m[*k], out);
            }
            out.push('}');
        }
        Value::Array(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical_json(item, out);
            }
            out.push(']');
        }
        other => out.push_str(&other.to_string()),
    }
}

/// SHA-256 (hex) over the canonical JSON of a tool definition.
pub fn definition_hash(tool: &Value) -> String {
    let mut s = String::new();
    canonical_json(tool, &mut s);
    hex::encode(Sha256::digest(s.as_bytes()))
}

fn tools_of(v: &Value) -> Option<&Vec<Value>> {
    v.pointer("/result/tools")
        .or_else(|| v.get("tools"))
        .and_then(|t| t.as_array())
}

fn version_of(v: &Value) -> Option<&str> {
    v.pointer("/result/serverInfo/version")
        .or_else(|| v.pointer("/serverInfo/version"))
        .or_else(|| v.pointer("/server_info/version"))
        .and_then(|x| x.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty() && s.len() <= MAX_VERSION_LEN)
}

fn index(tools: &[Value]) -> HashMap<String, String> {
    tools
        .iter()
        .filter_map(|t| Some((t.get("name")?.as_str()?.to_owned(), definition_hash(t))))
        .collect()
}

/// Remember the version an upstream announced in its `initialize` result.
pub fn record_initialize(server_id: &str, resp: &Value) {
    if let Some(ver) = version_of(resp) {
        versions().insert(server_id.to_owned(), ver.to_owned());
    }
}

/// Remember the definitions an upstream listed. Called with the *unfiltered*
/// response: the hash must describe what the server offers, not what one
/// group is allowed to see of it.
pub fn record_tools_list(server_id: &str, resp: &Value) {
    if let Some(tools) = tools_of(resp) {
        definitions().insert(server_id.to_owned(), Arc::new(index(tools)));
    }
}

/// What this instance already knows, without touching the database:
/// `(server_version, tool_definition_hash, definitions_known)`. The third
/// value separates "no listing seen yet" from "listed, but no such tool".
pub fn cached(server_id: &str, tool: &str) -> (Option<String>, Option<String>, bool) {
    let version = versions().get(server_id);
    match definitions().get(server_id) {
        Some(defs) => (version, defs.get(tool).cloned(), true),
        None => (version, None, false),
    }
}

/// Provenance for one call: memory first, then the control plane's latest
/// capability snapshot for the server. The snapshot result is cached either
/// way — including an empty one — so a server with no snapshot is asked
/// about once per TTL, not once per call.
pub async fn lookup(
    pool: &PgPool,
    tenant_id: &str,
    server_id: &str,
    tool: &str,
) -> (Option<String>, Option<String>) {
    let (version, hash, known) = cached(server_id, tool);
    if known {
        return (version, hash);
    }
    let (snap_version, defs) = load_snapshot(pool, tenant_id, server_id).await;
    let defs = Arc::new(defs);
    definitions().insert(server_id.to_owned(), defs.clone());
    let version = version.or_else(|| {
        let v = snap_version?;
        versions().insert(server_id.to_owned(), v.clone());
        Some(v)
    });
    (version, defs.get(tool).cloned())
}

async fn load_snapshot(
    pool: &PgPool,
    tenant_id: &str,
    server_id: &str,
) -> (Option<String>, HashMap<String, String>) {
    let row: Result<Option<(Value,)>, sqlx::Error> = sqlx::query_as(
        "SELECT capabilities FROM mcp_capability_snapshots \
         WHERE tenant_id = $1 AND mcp_server_id = $2 \
         ORDER BY snapshot_at DESC LIMIT 1",
    )
    .bind(tenant_id)
    .bind(server_id)
    .fetch_optional(pool)
    .await;
    match row {
        Ok(Some((caps,))) => {
            let version = version_of(&caps).map(str::to_owned);
            let defs = tools_of(&caps).map(|t| index(t)).unwrap_or_default();
            (version, defs)
        }
        Ok(None) => (None, HashMap::new()),
        Err(e) => {
            tracing::warn!(server_id, error = %e, "tool provenance: capability snapshot lookup failed");
            (None, HashMap::new())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn canonical_json_sorts_keys_at_every_depth() {
        let a = json!({"b": 1, "a": {"y": [1, {"k": 2, "j": 3}], "x": null}});
        let b = json!({"a": {"x": null, "y": [1, {"j": 3, "k": 2}]}, "b": 1});
        let (mut sa, mut sb) = (String::new(), String::new());
        canonical_json(&a, &mut sa);
        canonical_json(&b, &mut sb);
        assert_eq!(sa, sb);
        assert_eq!(sa, r#"{"a":{"x":null,"y":[1,{"j":3,"k":2}]},"b":1}"#);
    }

    #[test]
    fn the_definition_hash_is_pinned() {
        // A tool definition's hash is compared across releases and across
        // instances; if this changes, every stored hash silently stops
        // matching the live definition.
        let tool = json!({"name": "get_order", "description": "Look up an order",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}});
        assert_eq!(
            definition_hash(&tool),
            "4653538b0e55911dbc661c55961a9f8de1e27c7ae950acb842d82ede364e0319",
            "tool definition hash format changed — every stored hash stops matching",
        );
        // key order is not part of the definition
        let reordered = json!({"inputSchema": {"properties": {"id": {"type": "string"}}, "type": "object"},
            "description": "Look up an order", "name": "get_order"});
        assert_eq!(definition_hash(&tool), definition_hash(&reordered));
        // and a description change is a different definition
        let mut changed = tool.clone();
        changed["description"] = json!("Look up an order (and its refunds)");
        assert_ne!(definition_hash(&tool), definition_hash(&changed));
    }

    #[test]
    fn listing_then_calling_yields_the_hash_and_version() {
        let sid = format!("srv-{}", ulid::Ulid::new());
        assert_eq!(cached(&sid, "t"), (None, None, false));
        record_initialize(
            &sid,
            &json!({"result": {"serverInfo": {"name": "crm", "version": "2.4.1"}}}),
        );
        record_tools_list(
            &sid,
            &json!({"result": {"tools": [
            {"name": "t", "description": "d", "inputSchema": {"type": "object"}},
            {"name": "u", "inputSchema": {"type": "object"}}]}}),
        );
        let (v, h, known) = cached(&sid, "t");
        assert_eq!(v.as_deref(), Some("2.4.1"));
        assert!(known);
        assert_eq!(
            h,
            Some(definition_hash(
                &json!({"name": "t", "description": "d", "inputSchema": {"type": "object"}})
            ))
        );
        // listed, but no such tool: known, no hash — not a guess
        assert_eq!(cached(&sid, "nope"), (Some("2.4.1".into()), None, true));
    }

    #[test]
    fn an_absurd_version_is_not_recorded() {
        let sid = format!("srv-{}", ulid::Ulid::new());
        record_initialize(
            &sid,
            &json!({"result": {"serverInfo": {"version": "x".repeat(65)}}}),
        );
        assert_eq!(cached(&sid, "t").0, None);
        record_initialize(&sid, &json!({"result": {"serverInfo": {"version": "  "}}}));
        assert_eq!(cached(&sid, "t").0, None);
    }
}
