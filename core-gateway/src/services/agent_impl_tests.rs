// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Unit tests for [`super`] (precedence, normalisation,
//! client labels, fingerprint encoding).

use super::*;
use axum::http::HeaderValue;
use proptest::prelude::*;
use serde_json::json;

fn rel(name: &str, version: &str) -> Release {
    Release {
        name: Some(name.into()),
        version: Some(version.into()),
        build: None,
    }
}

fn carrier_release(c: Carrier) -> Release {
    match c {
        Carrier::Token => Release {
            name: Some("tok".into()),
            version: Some("1.0.0".into()),
            build: Some("sha256:aa".into()),
        },
        Carrier::McpClientInfo => rel("mcp", "2.0.0"),
        Carrier::Baggage => rel("bag", "3.0.0"),
        Carrier::Header => rel("hdr", "4.0.0"),
    }
}

fn set(c: &mut Carriers, which: Carrier, r: Release) {
    match which {
        Carrier::Token => c.token = Some(r),
        Carrier::McpClientInfo => c.mcp_client_info = Some(r),
        Carrier::Baggage => c.baggage = Some(r),
        Carrier::Header => c.header = Some(r),
    }
}

// -- precedence -------------------------------------------------------------

/// Every one of the 16 carrier combinations resolves to the highest-trust
/// carrier present, and the tuple is taken whole from it.
#[test]
fn every_carrier_combination_resolves_to_the_highest_present() {
    for mask in 0u8..16 {
        let mut c = Carriers::default();
        // The order is spelled out here, NOT read from `Carrier::PRECEDENCE`,
        // so a reordering of the implementation fails this test.
        let rfc_order = [
            Carrier::Token,
            Carrier::McpClientInfo,
            Carrier::Baggage,
            Carrier::Header,
        ];
        let present: Vec<Carrier> = rfc_order
            .iter()
            .enumerate()
            .filter(|(i, _)| mask & (1 << i) != 0)
            .map(|(_, c)| *c)
            .collect();
        for p in &present {
            set(&mut c, *p, carrier_release(*p));
        }
        let got = c.resolve();
        match present.first() {
            None => assert_eq!(got, None, "mask {mask:04b}: nothing present"),
            Some(top) => {
                let got = got.expect("resolved");
                let want = carrier_release(*top);
                assert_eq!(got.source, top.source(), "mask {mask:04b}");
                assert_eq!(got.name, want.name, "mask {mask:04b}: tuple not mixed");
                assert_eq!(got.version, want.version);
                assert_eq!(got.build, want.build);
            }
        }
    }
}

#[test]
fn a_declared_value_never_overrides_a_verified_claim() {
    let mut c = Carriers::default();
    set(&mut c, Carrier::Token, rel("verified-agent", "1.0.0"));
    for other in [Carrier::McpClientInfo, Carrier::Baggage, Carrier::Header] {
        set(&mut c, other, rel("spoof", "9.9.9"));
    }
    let r = c.resolve().unwrap();
    assert_eq!(r.name.as_deref(), Some("verified-agent"));
    assert_eq!(r.trust(), "verified");
}

fn built(name: &str, version: &str, build: &str) -> Release {
    Release {
        build: Some(build.into()),
        ..rel(name, version)
    }
}

#[test]
fn a_build_the_winner_cannot_carry_comes_from_a_carrier_naming_the_same_release() {
    // MCP clientInfo has no build field; the headers name the same release.
    let c = Carriers {
        mcp_client_info: Some(rel("screening-agent", "1.4.2")),
        header: Some(built("screening-agent", "1.4.2", "defbe1a-dirty")),
        ..Default::default()
    };
    let r = c.resolve().unwrap();
    assert_eq!(r.source, "mcp_client_info", "name/version source unchanged");
    assert_eq!(r.build.as_deref(), Some("defbe1a-dirty"));
}

#[test]
fn a_build_is_never_taken_from_a_carrier_naming_another_release() {
    for other in [
        built("screening-agent", "1.4.3", "abc"), // another version
        built("other-agent", "1.4.2", "abc"),     // another name
        Release {
            name: None,
            version: Some("1.4.2".into()),
            build: Some("abc".into()),
        }, // does not name the software
    ] {
        let c = Carriers {
            mcp_client_info: Some(rel("screening-agent", "1.4.2")),
            header: Some(other.clone()),
            ..Default::default()
        };
        assert_eq!(c.resolve().unwrap().build, None, "{other:?}");
    }
}

#[test]
fn the_highest_matching_lower_carrier_supplies_the_build() {
    // Baggage names another version (skipped); the header matches.
    let c = Carriers {
        mcp_client_info: Some(rel("a", "1")),
        baggage: Some(built("a", "2", "bag-build")),
        header: Some(built("a", "1", "hdr-build")),
        ..Default::default()
    };
    assert_eq!(c.resolve().unwrap().build.as_deref(), Some("hdr-build"));
    // Both match: the higher (baggage) wins.
    let c = Carriers {
        mcp_client_info: Some(rel("a", "1")),
        baggage: Some(built("a", "1", "bag-build")),
        header: Some(built("a", "1", "hdr-build")),
        ..Default::default()
    };
    assert_eq!(c.resolve().unwrap().build.as_deref(), Some("bag-build"));
    // The winner's own build is never replaced.
    let c = Carriers {
        mcp_client_info: Some(built("a", "1", "own")),
        header: Some(built("a", "1", "hdr-build")),
        ..Default::default()
    };
    assert_eq!(c.resolve().unwrap().build.as_deref(), Some("own"));
}

#[test]
fn a_declared_build_refines_a_verified_release_without_raising_its_trust() {
    let c = Carriers {
        token: Some(rel("fraud-screener", "2.1.0")),
        header: Some(built("fraud-screener", "2.1.0", "sha-9f1")),
        ..Default::default()
    };
    let r = c.resolve().unwrap();
    assert_eq!(r.build.as_deref(), Some("sha-9f1"));
    assert_eq!(r.source, "token");
    assert_eq!(r.trust(), "verified", "trust is the name/version source's");
}

#[test]
fn a_carrier_with_only_a_build_is_not_present() {
    let c = Carriers {
        baggage: Some(Release {
            build: Some("abc".into()),
            ..Default::default()
        }),
        header: Some(rel("hdr", "1")),
        ..Default::default()
    };
    assert_eq!(c.resolve().unwrap().source, "header");
}

#[test]
fn only_the_token_carrier_is_verified() {
    for c in Carrier::PRECEDENCE {
        let expected = if c == Carrier::Token {
            "verified"
        } else {
            "declared"
        };
        assert_eq!(c.trust(), expected);
        assert_eq!(trust_for_source(c.source()), expected);
    }
}

// -- normalisation ----------------------------------------------------------

#[test]
fn normalisation_examples() {
    assert_eq!(
        normalise_name(Some("  My-Agent.v2 ")),
        Ok(Some("my-agent.v2".into()))
    );
    assert_eq!(normalise_name(Some("")), Ok(None));
    assert_eq!(normalise_name(None), Ok(None));
    assert_eq!(normalise_name(Some("has space")), Err(Illegal));
    assert_eq!(normalise_name(Some("ünicode")), Err(Illegal));
    assert_eq!(normalise_name(Some(&"a".repeat(65))), Err(Illegal));
    assert_eq!(
        normalise_name(Some(&"a".repeat(64))),
        Ok(Some("a".repeat(64)))
    );
    assert_eq!(
        normalise_version(Some("1.4.2+build.7")),
        Ok(Some("1.4.2+build.7".into()))
    );
    assert_eq!(normalise_version(Some("1.4 beta")), Err(Illegal));
    assert_eq!(
        normalise_build(Some("sha256:0123abcd")),
        Ok(Some("sha256:0123abcd".into()))
    );
    assert_eq!(normalise_build(Some("a+b")), Err(Illegal));
    assert_eq!(normalise_build(Some(&"f".repeat(129))), Err(Illegal));
}

#[test]
fn an_illegal_value_is_dropped_and_counted_never_stored_raw() {
    let mut drops = Drops::default();
    let r = normalise_release(
        "header",
        Some("bad name"),
        Some("1.0"),
        Some("x+y"),
        &mut drops,
    );
    assert_eq!(r.name, None);
    assert_eq!(r.version.as_deref(), Some("1.0"));
    assert_eq!(r.build, None);
    assert_eq!(
        drops.0,
        vec![
            Dropped {
                carrier: "header",
                field: Field::Name
            },
            Dropped {
                carrier: "header",
                field: Field::Build
            },
        ]
    );
}

fn legal_name(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b))
}
fn legal_version(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+_-".contains(&b))
}
fn legal_build(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b":._-".contains(&b))
}

proptest! {
    /// No stored value ever violates the bounds; every non-blank value that
    /// is not stored is counted as a drop.
    #[test]
    fn no_stored_value_violates_the_bounds(
        name in ".{0,80}", version in ".{0,80}", build in ".{0,150}",
    ) {
        let mut drops = Drops::default();
        let r = normalise_release("baggage", Some(&name), Some(&version), Some(&build), &mut drops);
        let mut expected_drops = 0;
        for (raw, stored, legal) in [
            (&name, &r.name, legal_name as fn(&str) -> bool),
            (&version, &r.version, legal_version),
            (&build, &r.build, legal_build),
        ] {
            match stored {
                Some(v) => prop_assert!(legal(v), "stored {v:?} violates its bound"),
                None if !raw.trim().is_empty() => expected_drops += 1,
                None => {}
            }
        }
        prop_assert_eq!(drops.0.len(), expected_drops);
    }

    /// Whatever the headers say, every stored proxy_logs value is legal.
    #[test]
    fn carriers_from_arbitrary_headers_store_only_legal_values(
        n in "[ -~]{0,90}", v in "[ -~]{0,90}", b in "[ -~]{0,140}", ua in "[ -~]{0,60}",
    ) {
        let mut h = HeaderMap::new();
        for (k, val) in [
            ("x-brutor-agent-name", &n), ("x-brutor-agent-version", &v),
            ("x-brutor-agent-build", &b), ("user-agent", &ua),
        ] {
            if let Ok(hv) = HeaderValue::from_str(val) { h.insert(k, hv); }
        }
        let c = Carriers::from_headers(&h, &mut Drops::default());
        if let Some(r) = c.resolve() {
            prop_assert!(r.name.as_deref().is_none_or(legal_name));
            prop_assert!(r.version.as_deref().is_none_or(legal_version));
            prop_assert!(r.build.as_deref().is_none_or(legal_build));
        }
        prop_assert!(c.client_label.as_deref().is_none_or(legal_name));
    }
}

// -- client labels ----------------------------------------------------------

#[test]
fn client_label_cases() {
    let mut d = Drops::default();
    assert_eq!(
        client_label(None, Some("python-httpx/0.28.1"), &mut d).as_deref(),
        Some("python-httpx")
    );
    assert_eq!(
        client_label(
            None,
            Some("Mozilla/5.0 (Macintosh; Intel Mac OS X)"),
            &mut d
        )
        .as_deref(),
        Some("browser")
    );
    assert_eq!(
        client_label(None, Some("curl/8.4.0"), &mut d).as_deref(),
        Some("curl")
    );
    assert_eq!(
        client_label(None, Some("Go-http-client/1.1"), &mut d).as_deref(),
        Some("go-http-client")
    );
    // The explicit header wins over any User-Agent.
    assert_eq!(
        client_label(Some("Portal"), Some("python-httpx/0.28.1"), &mut d).as_deref(),
        Some("portal")
    );
    assert!(d.0.is_empty());
    // An illegal explicit label is dropped (counted) and the UA answers.
    assert_eq!(
        client_label(Some("not a label"), Some("curl/8"), &mut d).as_deref(),
        Some("curl")
    );
    assert_eq!(d.0.len(), 1);
    assert_eq!(client_label(None, None, &mut d), None);
    assert_eq!(client_label(None, Some("   "), &mut d), None);
}

// -- carriers ---------------------------------------------------------------

#[test]
fn baggage_members_are_parsed_decoded_and_first_wins() {
    let raw = parse_baggage([
        "other=1, gen_ai.agent.name = screening%2Dagent ;prop=x",
        "gen_ai.agent.version=1.4.2,gen_ai.agent.name=second,gen_ai.agent.build=sha256%3Aab",
    ]);
    assert_eq!(raw.name.as_deref(), Some("screening-agent"));
    assert_eq!(raw.version.as_deref(), Some("1.4.2"));
    assert_eq!(raw.build.as_deref(), Some("sha256:ab"));
}

#[test]
fn headers_yield_baggage_header_and_label_carriers() {
    let mut h = HeaderMap::new();
    h.insert(
        "baggage",
        HeaderValue::from_static("gen_ai.agent.name=bag-agent,gen_ai.agent.version=2.0"),
    );
    h.insert("x-brutor-agent-name", HeaderValue::from_static("Hdr-Agent"));
    h.insert("x-brutor-agent-version", HeaderValue::from_static("1.0.0"));
    h.insert(
        "user-agent",
        HeaderValue::from_static("python-httpx/0.28.1"),
    );
    let c = Carriers::from_headers(&h, &mut Drops::default());
    assert_eq!(c.baggage, Some(rel("bag-agent", "2.0")));
    assert_eq!(c.header, Some(rel("hdr-agent", "1.0.0")));
    assert_eq!(c.client_label.as_deref(), Some("python-httpx"));
    assert_eq!(c.user_agent.as_deref(), Some("python-httpx/0.28.1"));
    // Baggage outranks the Brutor headers.
    assert_eq!(c.resolve().unwrap().source, "baggage");
}

#[test]
fn mcp_client_info_prefers_meta_then_initialize() {
    let meta = json!({"jsonrpc":"2.0","method":"tools/call","params":{
        "name":"t","_meta":{"io.modelcontextprotocol/clientInfo":{"name":"a","version":"1"}}}});
    assert_eq!(mcp_client_info(&meta).unwrap().name.as_deref(), Some("a"));
    let init = json!({"jsonrpc":"2.0","method":"initialize","params":{
        "clientInfo":{"name":"b","version":"2"}}});
    assert_eq!(
        mcp_client_info(&init).unwrap().version.as_deref(),
        Some("2")
    );
    // clientInfo outside initialize is not a declaration.
    let stray = json!({"method":"tools/list","params":{"clientInfo":{"name":"c"}}});
    assert!(mcp_client_info(&stray).is_none());
    let batch = json!([{"method":"ping","params":{}}, init]);
    assert_eq!(mcp_client_info(&batch).unwrap().name.as_deref(), Some("b"));
    // A non-string value is present-but-illegal: normalisation counts it.
    let bad = json!({"method":"initialize","params":{"clientInfo":{"name":7}}});
    let mut d = Drops::default();
    assert!(
        mcp_client_info(&bad)
            .unwrap()
            .normalise(Carrier::McpClientInfo, &mut d)
            .is_none()
    );
    assert_eq!(d.0.len(), 1);
}

#[test]
fn verified_claims_only_attach_when_present() {
    let base = Carriers::default();
    assert_eq!(base.clone().with_verified_token(None).token, None);
    let r = release_from_claims(Some("Screening-Agent"), Some("1.0"), None).unwrap();
    assert_eq!(r.name.as_deref(), Some("screening-agent"));
    let c = base.with_verified_token(Some(r));
    assert_eq!(c.resolve().unwrap().source, "token");
    assert!(release_from_claims(None, None, Some("abc")).is_none());
}

#[tokio::test]
async fn capture_reads_mcp_client_info_and_returns_the_body_intact() {
    let store = SessionStore::new(None);
    let body = json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "clientInfo":{"name":"demo-agent","version":"0.3.1"}}})
    .to_string();
    let req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/mcp/srv-1")
        .header("x-api-key", "k-capture-test")
        .body(Body::from(body.clone()))
        .unwrap();
    let (req, c, pending) = capture(req, &store).await.expect("captured");
    assert_eq!(c.mcp_client_info, Some(rel("demo-agent", "0.3.1")));
    let bytes = axum::body::to_bytes(req.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes, body.as_bytes(), "body forwarded unchanged");

    // The server's session id binds the declaration for later requests.
    let mut resp = HeaderMap::new();
    resp.insert("mcp-session-id", HeaderValue::from_static("sess-capture-1"));
    bind_session(&store, &pending.expect("initialize is pending"), &resp).await;
    let later = || {
        Request::builder()
            .method("POST")
            .uri("/v1/proxy/mcp/srv-1")
            .header("x-api-key", "k-capture-test")
            .header("mcp-session-id", "sess-capture-1")
            .body(Body::from(
                r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
            ))
            .unwrap()
    };
    let (_, c, _) = capture(later(), &store).await.unwrap();
    assert_eq!(c.mcp_client_info, Some(rel("demo-agent", "0.3.1")));

    // A different credential naming the same session does not inherit it.
    let other = Request::builder()
        .method("POST")
        .uri("/v1/proxy/mcp/srv-1")
        .header("x-api-key", "someone-else")
        .header("mcp-session-id", "sess-capture-1")
        .body(Body::from(
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/list"}"#,
        ))
        .unwrap();
    let (_, c, _) = capture(other, &store).await.unwrap();
    assert_eq!(c.mcp_client_info, None);

    // DELETE ends the session and its declaration.
    let delete = Request::builder()
        .method("DELETE")
        .uri("/v1/proxy/mcp/srv-1")
        .header("x-api-key", "k-capture-test")
        .header("mcp-session-id", "sess-capture-1")
        .body(Body::empty())
        .unwrap();
    capture(delete, &store).await.unwrap();
    let (_, c, _) = capture(later(), &store).await.unwrap();
    assert_eq!(c.mcp_client_info, None, "deleted with the session");
}

// -- session keys ------------------------------------------------------------

fn scoped(tenant: &str, credential: &str) -> SessionScope {
    let mut h = HeaderMap::new();
    h.insert("x-tenant-id", HeaderValue::from_str(tenant).unwrap());
    h.insert("x-api-key", HeaderValue::from_str(credential).unwrap());
    SessionScope::of(&h)
}

#[test]
fn a_session_key_is_namespaced_and_names_tenant_credential_hash_and_session() {
    let secret = "sk_brutor_api_live_0123456789abcdef";
    let key = scoped("acme", secret).key("sess-1");
    let k = key.as_str();
    assert!(k.starts_with("brutor:mcp_client_info:v1:acme:"), "{k}");
    assert!(k.ends_with(":sess-1"), "{k}");
    assert!(
        k.contains(&credential_hash(secret.as_bytes())),
        "keyed by the credential's hash: {k}"
    );
}

#[test]
fn the_raw_credential_never_reaches_the_key() {
    for secret in [
        "sk_brutor_api_live_0123456789abcdef",
        "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x",
        "short",
    ] {
        let k = scoped("acme", secret).key("s").as_str().to_string();
        assert!(!k.contains(secret), "raw credential in {k}");
        for part in secret.split(|c: char| !c.is_ascii_alphanumeric()) {
            if part.len() >= 8 {
                assert!(!k.contains(part), "credential fragment {part} in {k}");
            }
        }
    }
    // Domain-separated: not the bare sha256 of the credential, so a key
    // cannot be matched against a table of hashed API keys.
    let bare = hex::encode(Sha256::digest(b"short"));
    assert_ne!(credential_hash(b"short"), bare);
}

#[test]
fn session_keys_separate_callers_tenants_and_sessions() {
    let a = scoped("acme", "k1").key("s");
    assert_eq!(a, scoped("acme", "k1").key("s"), "deterministic");
    assert_ne!(a, scoped("acme", "k2").key("s"), "another credential");
    assert_ne!(a, scoped("other", "k1").key("s"), "another tenant");
    assert_ne!(a, scoped("acme", "k1").key("t"), "another session");
    // The separator is escaped. Unescaped, a tenant that embeds another
    // caller's `:<hash>:` segment and a session id that embeds one collide:
    //   (tenant "a",          cred k1, session "x:<h2>:y") → a:<h1>:x:<h2>:y
    //   (tenant "a:<h1>:x",   cred k2, session "y")        → a:<h1>:x:<h2>:y
    let (h1, h2) = (credential_hash(b"k1"), credential_hash(b"k2"));
    assert_ne!(
        scoped("a", "k1").key(&format!("x:{h2}:y")).as_str(),
        scoped(&format!("a:{h1}:x"), "k2").key("y").as_str(),
        "two callers' keys must never collide"
    );
}

#[tokio::test]
async fn the_single_replica_store_binds_resolves_and_forgets() {
    let store = SessionStore::new(None);
    assert!(!store.is_shared());
    let key = scoped("t", "k").key("s");
    assert_eq!(store.resolve(&key).await, None);
    store.bind(&key, &rel("a", "1")).await;
    assert_eq!(store.resolve(&key).await, Some(rel("a", "1")));
    store.forget(&key).await;
    assert_eq!(store.resolve(&key).await, None);
    assert_eq!(store.unavailable_count(), 0);
}

#[tokio::test]
async fn capture_does_not_read_bodies_off_the_mcp_surfaces() {
    let req = Request::builder()
        .method("POST")
        .uri("/v1/proxy/llm/chat/completions")
        .body(Body::from(
            r#"{"method":"initialize","params":{"clientInfo":{"name":"x","version":"1"}}}"#,
        ))
        .unwrap();
    let (_, c, pending) = capture(req, &SessionStore::new(None)).await.unwrap();
    assert_eq!(c.mcp_client_info, None);
    assert!(pending.is_none());
}

// -- recording --------------------------------------------------------------

#[test]
fn stamp_writes_the_resolved_release_and_label() {
    let auth = crate::models::auth::make_auth_context(crate::models::auth::AuthType::ApiKey, "t");
    let mut log = crate::services::proxy_logger::build_proxy_log(
        "llm", &auth, "POST", "/x", None, None, 200, 1.0, true, None, "c",
    );
    let c = Carriers {
        header: Some(Release {
            name: Some("agent".into()),
            version: Some("1.2.3".into()),
            build: Some("abc123".into()),
        }),
        client_label: Some("python-httpx".into()),
        user_agent: Some("python-httpx/0.28.1".into()),
        ..Default::default()
    };
    stamp(&mut log, &c);
    assert_eq!(log.agent_impl_name.as_deref(), Some("agent"));
    assert_eq!(log.agent_impl_version.as_deref(), Some("1.2.3"));
    assert_eq!(log.agent_impl_build.as_deref(), Some("abc123"));
    assert_eq!(log.agent_impl_source.as_deref(), Some("header"));
    assert_eq!(log.client_label.as_deref(), Some("python-httpx"));
    assert_eq!(log.user_agent.as_deref(), Some("python-httpx/0.28.1"));
}

#[tokio::test]
async fn build_proxy_log_records_the_request_carriers_via_the_auth_snapshot() {
    let c = Carriers {
        baggage: Some(rel("bag", "1")),
        ..Default::default()
    };
    scope(c, async {
        let auth =
            crate::models::auth::make_auth_context(crate::models::auth::AuthType::ApiKey, "t");
        let log = crate::services::proxy_logger::build_proxy_log(
            "mcp", &auth, "POST", "/x", None, None, 200, 1.0, true, None, "c",
        );
        assert_eq!(log.agent_impl_source.as_deref(), Some("baggage"));
    })
    .await;
}

// -- fingerprint ------------------------------------------------------------

/// The canonical encoding is pinned by a fixed vector: a change to the
/// encoding must be a new domain version, not a silent edit.
#[test]
fn fingerprint_encoding_is_pinned() {
    let mut expected = Sha256::new();
    expected.update(b"brutor.implementation_fingerprint.v1");
    expected.update([1u8]);
    expected.update(19u32.to_be_bytes());
    expected.update(b"python-httpx/0.28.1");
    expected.update([0u8]); // no MCP name
    expected.update([0u8]); // no MCP version
    expected.update([1u8]);
    expected.update(4u32.to_be_bytes());
    expected.update(b"abcd");
    let want = format!("sha256:{}", hex::encode(expected.finalize()));
    let got =
        implementation_fingerprint(Some("python-httpx/0.28.1"), None, None, Some("abcd")).unwrap();
    assert_eq!(got, want);
    assert_eq!(
        got.len(),
        71,
        "fits ai_system_runs.implementation_fingerprint"
    );
    assert_eq!(
        got,
        "sha256:".to_string()
            + &hex::encode(Sha256::digest(
                [
                    &b"brutor.implementation_fingerprint.v1"[..],
                    &[1, 0, 0, 0, 19],
                    b"python-httpx/0.28.1",
                    &[0, 0, 1, 0, 0, 0, 4],
                    b"abcd",
                ]
                .concat()
            ))
    );
}

#[test]
fn fingerprint_is_injective_across_field_boundaries() {
    // A separator-based encoding would collide these two.
    let a = implementation_fingerprint(Some("ab"), Some("c"), None, None);
    let b = implementation_fingerprint(Some("a"), Some("bc"), None, None);
    assert_ne!(a, b);
    // Absent differs from empty.
    assert_ne!(
        implementation_fingerprint(Some(""), None, None, None),
        implementation_fingerprint(None, Some(""), None, None)
    );
    assert_eq!(implementation_fingerprint(None, None, None, None), None);
}

#[test]
fn baggage_is_the_only_caller_only_header() {
    assert!(is_caller_only_header("baggage"));
    assert!(is_caller_only_header("Baggage"));
    assert!(!is_caller_only_header("traceparent"));
}
