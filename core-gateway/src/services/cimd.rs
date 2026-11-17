// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! CIMD — Client ID Metadata Documents (the successor to DCR
//! for MCP clients; see modelcontextprotocol PR #2858).
//!
//! A CIMD client presents `client_id = https://…` pointing at a JSON
//! metadata document it hosts (redirect_uris, client_name, …). No
//! registration round-trip. The gateway's inbound OAuth is a *facade*
//! over the tenant's IdP, which knows nothing about URL client ids —
//! so CIMD is honored by **bridging**:
//!
//!   1. `GET …/authorize` (gateway): fetch + validate the CIMD doc
//!      (SSRF-guarded), lazily DCR ONE gateway-owned client per
//!      (idp, cimd_url) at the IdP, stash the client's redirect/PKCE in
//!      Redis, and 302 the user-agent to the IdP with the gateway's own
//!      client + callback + PKCE.
//!   2. `GET …/callback` (gateway): swap the IdP's code for a
//!      gateway-minted `gwc_…` code, 302 back to the CIMD client's
//!      redirect_uri.
//!   3. `POST …/token`: a `gwc_…` code verifies the CLIENT's PKCE at the
//!      gateway, then exchanges the stashed upstream code with the IdP
//!      using the gateway client's credentials + PKCE.
//!
//! Non-CIMD client ids pass through `…/authorize` untouched (302 to the
//! IdP with the original query), so existing registered clients keep
//! their exact flow.

use std::net::IpAddr;

use base64::Engine as _;
use rand::Rng as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// Max CIMD document size. Matches the DCR body cap in `register_client`.
const MAX_DOC_BYTES: usize = 64 * 1024;
/// Authorize→callback window.
pub const BRIDGE_TTL_SECS: u64 = 600;
/// Callback→token window (single use).
pub const CODE_TTL_SECS: u64 = 120;

pub const BRIDGE_KEY_PREFIX: &str = "oauth:cimd:bridge:";
pub const CODE_KEY_PREFIX: &str = "oauth:cimd:code:";
/// Gateway-minted authorization codes carry this prefix so the token
/// endpoint can tell a bridged exchange from a direct IdP code.
pub const GW_CODE_PREFIX: &str = "gwc_";

/// A CIMD client id is an absolute https URL (the location of the
/// metadata document). Anything else is a regular registered client id.
pub fn is_cimd_client_id(client_id: &str) -> bool {
    client_id.starts_with("https://") && reqwest::Url::parse(client_id).is_ok()
}

/// Validated subset of a CIMD document.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CimdDoc {
    pub client_id: String,
    pub redirect_uris: Vec<String>,
    pub client_name: Option<String>,
}

/// Validate a fetched metadata document against the URL it was fetched
/// from. Spec: the document's `client_id` MUST equal its own URL;
/// `redirect_uris` is required. Public clients only in v1 —
/// `token_endpoint_auth_method`, when present, must be `none` (the
/// bridge supplies its own confidential client upstream).
pub fn validate_doc(url: &str, doc: &Value) -> Result<CimdDoc, String> {
    let doc_client_id = doc
        .get("client_id")
        .and_then(|v| v.as_str())
        .ok_or("CIMD document is missing client_id")?;
    if doc_client_id != url {
        return Err(format!(
            "CIMD document client_id '{doc_client_id}' does not match its URL '{url}'"
        ));
    }
    let redirect_uris: Vec<String> = doc
        .get("redirect_uris")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|u| u.as_str())
                .map(String::from)
                .collect()
        })
        .unwrap_or_default();
    if redirect_uris.is_empty() {
        return Err("CIMD document has no redirect_uris".into());
    }
    if let Some(method) = doc
        .get("token_endpoint_auth_method")
        .and_then(|v| v.as_str())
    {
        if method != "none" {
            return Err(format!(
                "CIMD clients are public clients; token_endpoint_auth_method '{method}' is not supported"
            ));
        }
    }
    Ok(CimdDoc {
        client_id: doc_client_id.to_string(),
        redirect_uris,
        client_name: doc
            .get("client_name")
            .and_then(|v| v.as_str())
            .map(String::from),
    })
}

/// SSRF guard: addresses a CIMD document must never resolve to.
/// Loopback, RFC 1918, link-local (incl. 169.254.169.254 metadata),
/// CGNAT 100.64/10, multicast/broadcast/unspecified, and their IPv6
/// analogues (incl. IPv4-mapped forms).
pub fn ip_blocked(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            v4.is_loopback()
                || v4.is_private()
                || v4.is_link_local()
                || v4.is_broadcast()
                || v4.is_unspecified()
                || v4.is_multicast()
                || v4.is_documentation()
                // CGNAT 100.64.0.0/10
                || (v4.octets()[0] == 100 && (v4.octets()[1] & 0b1100_0000) == 64)
        }
        IpAddr::V6(v6) => {
            if let Some(mapped) = v6.to_ipv4_mapped() {
                return ip_blocked(IpAddr::V4(mapped));
            }
            v6.is_loopback()
                || v6.is_unspecified()
                || v6.is_multicast()
                // link-local fe80::/10
                || (v6.segments()[0] & 0xffc0) == 0xfe80
                // unique-local fc00::/7
                || (v6.segments()[0] & 0xfe00) == 0xfc00
        }
    }
}

/// Fetch a CIMD document with SSRF protections: https only, DNS
/// pre-resolved with every address vetted and the connection pinned to
/// a vetted address (no rebinding window), no redirects, 10s timeout,
/// 64 KiB cap, JSON body.
pub async fn fetch_document(url: &str) -> Result<Value, String> {
    let parsed = reqwest::Url::parse(url).map_err(|e| format!("Invalid CIMD URL: {e}"))?;
    if parsed.scheme() != "https" {
        return Err("CIMD client_id must be an https URL".into());
    }
    let host = parsed.host_str().ok_or("CIMD URL has no host")?.to_string();
    let port = parsed.port_or_known_default().unwrap_or(443);

    let addrs: Vec<std::net::SocketAddr> = tokio::net::lookup_host((host.as_str(), port))
        .await
        .map_err(|e| format!("CIMD host resolution failed: {e}"))?
        .collect();
    if addrs.is_empty() {
        return Err("CIMD host resolved to no addresses".into());
    }
    if let Some(bad) = addrs.iter().find(|a| ip_blocked(a.ip())) {
        return Err(format!(
            "CIMD host resolves to a blocked address ({}) — refusing to fetch",
            bad.ip()
        ));
    }

    let client = crate::services::outbound_policy::client_builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(10))
        // Pin the connection to the vetted address — DNS can't rebind
        // between the check and the request.
        .resolve(&host, addrs[0])
        .build()
        .map_err(|e| format!("HTTP client build failed: {e}"))?;

    let resp = client
        .get(parsed)
        .header("accept", "application/json")
        .send()
        .await
        .map_err(|e| format!("CIMD document fetch failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("CIMD document fetch returned {}", resp.status()));
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("CIMD document read failed: {e}"))?;
    if bytes.len() > MAX_DOC_BYTES {
        return Err(format!("CIMD document exceeds {MAX_DOC_BYTES} byte limit"));
    }
    serde_json::from_slice(&bytes).map_err(|e| format!("CIMD document is not valid JSON: {e}"))
}

// ── PKCE (RFC 7636, S256 only) ─────────────────────────────────────────

pub fn random_token(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    rand::rng().fill(&mut buf[..]);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(buf)
}

pub fn s256_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
}

pub fn verify_s256(verifier: &str, challenge: &str) -> bool {
    // Constant-time not required — the challenge is not a secret (it
    // traveled in the authorize redirect); correctness is what matters.
    s256_challenge(verifier) == challenge
}

// ── Bridge state (Redis-persisted between the three legs) ──────────────

/// Everything the callback + token legs need, written at authorize time.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BridgeState {
    pub tenant_id: String,
    pub mcp_server_id: String,
    pub idp_server_id: String,
    pub cimd_url: String,
    pub client_redirect_uri: String,
    pub client_state: Option<String>,
    pub client_code_challenge: String,
    pub gateway_verifier: String,
    pub gateway_callback: String,
    pub idp_client_id: String,
    /// Encrypted with the platform EncryptionService; None for IdPs
    /// that issued a public gateway client.
    pub idp_client_secret_enc: Option<String>,
}

/// BridgeState + the IdP's authorization code, written at callback time
/// under the gateway-minted `gwc_…` code.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodeState {
    pub bridge: BridgeState,
    pub upstream_code: String,
}

pub async fn redis_put_json<T: Serialize>(
    redis: &mut redis::aio::ConnectionManager,
    key: &str,
    value: &T,
    ttl_secs: u64,
) -> Result<(), String> {
    let payload = serde_json::to_string(value).map_err(|e| e.to_string())?;
    redis::cmd("SET")
        .arg(key)
        .arg(payload)
        .arg("EX")
        .arg(ttl_secs)
        .query_async::<()>(redis)
        .await
        .map_err(|e| e.to_string())
}

/// Single-use read: GETDEL so a replayed code/state finds nothing.
pub async fn redis_take_json<T: for<'de> Deserialize<'de>>(
    redis: &mut redis::aio::ConnectionManager,
    key: &str,
) -> Result<Option<T>, String> {
    let raw: Option<String> = redis::cmd("GETDEL")
        .arg(key)
        .query_async(redis)
        .await
        .map_err(|e| e.to_string())?;
    match raw {
        None => Ok(None),
        Some(s) => serde_json::from_str(&s)
            .map(Some)
            .map_err(|e| e.to_string()),
    }
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn cimd_client_id_detection() {
        assert!(is_cimd_client_id(
            "https://cli.example.com/oauth/client.json"
        ));
        assert!(!is_cimd_client_id("http://cli.example.com/client.json"));
        assert!(!is_cimd_client_id("my-registered-client"));
        assert!(!is_cimd_client_id("https://"));
    }

    #[test]
    fn doc_validation_happy_path() {
        let url = "https://cli.example.com/client.json";
        let doc = json!({
            "client_id": url,
            "client_name": "Example CLI",
            "redirect_uris": ["http://127.0.0.1:33418/callback", "http://localhost:33418/callback"],
            "token_endpoint_auth_method": "none"
        });
        let parsed = validate_doc(url, &doc).unwrap();
        assert_eq!(parsed.client_id, url);
        assert_eq!(parsed.redirect_uris.len(), 2);
        assert_eq!(parsed.client_name.as_deref(), Some("Example CLI"));
    }

    #[test]
    fn doc_validation_rejects_mismatched_client_id() {
        let doc = json!({
            "client_id": "https://evil.example.com/client.json",
            "redirect_uris": ["http://127.0.0.1:1/cb"]
        });
        assert!(
            validate_doc("https://cli.example.com/client.json", &doc)
                .unwrap_err()
                .contains("does not match")
        );
    }

    #[test]
    fn doc_validation_rejects_missing_redirects_and_confidential_clients() {
        let url = "https://cli.example.com/client.json";
        assert!(validate_doc(url, &json!({"client_id": url})).is_err());
        let confidential = json!({
            "client_id": url,
            "redirect_uris": ["http://127.0.0.1:1/cb"],
            "token_endpoint_auth_method": "client_secret_post"
        });
        assert!(
            validate_doc(url, &confidential)
                .unwrap_err()
                .contains("public clients")
        );
    }

    #[test]
    fn ssrf_ip_matrix() {
        let blocked = [
            "127.0.0.1",
            "10.0.0.8",
            "172.16.4.2",
            "192.168.1.1",
            "169.254.169.254", // cloud metadata
            "100.64.0.1",      // CGNAT
            "0.0.0.0",
            "255.255.255.255",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
        ];
        for ip in blocked {
            assert!(ip_blocked(ip.parse().unwrap()), "{ip} should be blocked");
        }
        let allowed = [
            "93.184.216.34",
            "8.8.8.8",
            "2606:2800:220:1:248:1893:25c8:1946",
        ];
        for ip in allowed {
            assert!(!ip_blocked(ip.parse().unwrap()), "{ip} should be allowed");
        }
    }

    #[test]
    fn pkce_s256_round_trip() {
        let verifier = random_token(32);
        let challenge = s256_challenge(&verifier);
        assert!(verify_s256(&verifier, &challenge));
        assert!(!verify_s256("wrong-verifier", &challenge));
        // RFC 7636 appendix B known vector
        assert_eq!(
            s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn bridge_state_serde_round_trip() {
        let b = BridgeState {
            tenant_id: "t".into(),
            mcp_server_id: "m".into(),
            idp_server_id: "i".into(),
            cimd_url: "https://cli.example.com/client.json".into(),
            client_redirect_uri: "http://127.0.0.1:1/cb".into(),
            client_state: Some("s".into()),
            client_code_challenge: "c".into(),
            gateway_verifier: "v".into(),
            gateway_callback: "https://gw/cb".into(),
            idp_client_id: "idp-client".into(),
            idp_client_secret_enc: None,
        };
        let round: BridgeState = serde_json::from_str(&serde_json::to_string(&b).unwrap()).unwrap();
        assert_eq!(round.cimd_url, b.cimd_url);
    }
}
