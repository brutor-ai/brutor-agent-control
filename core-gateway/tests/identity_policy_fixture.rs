// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The identity-policy resolver, the judge and the policy validation, held
//! to the shared cases in `tests/fixtures/identity_policy_resolver.json`.
//! The control plane's resolver reads the same file from this repository,
//! so both planes resolve every case the same way.

use brutor_gateway_core::models::auth::{CredentialAssurance, CredentialKind};
use brutor_gateway_core::services::identity_policy::{
    ChainNode, IdentityPolicy, IdentityPolicyDefault, JudgedCredential, POLICY_FIELDS,
    ResolverInput, judge_credential, resolve,
};
use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/identity_policy_resolver.json");

fn fixture() -> Value {
    let fixture: Value = serde_json::from_str(FIXTURE).expect("the fixture is JSON");
    assert_eq!(
        fixture["schema"], "brutor/identity-policy-resolver-cases/1",
        "the fixture's schema"
    );
    fixture
}

fn cases<'a>(fixture: &'a Value, name: &str) -> &'a Vec<Value> {
    let cases = fixture[name].as_array().expect("a list of cases");
    assert!(!cases.is_empty(), "{name} is empty");
    cases
}

fn policy(value: &Value) -> IdentityPolicy {
    IdentityPolicy::validate(value).unwrap_or_else(|e| panic!("{value}: {e:?}"))
}

/// An effective policy: as a stored one, except that an issuer list may be
/// empty (an intersection can leave none, which accepts no workload
/// credential).
fn effective_policy(value: &Value) -> IdentityPolicy {
    if value["allowed_issuers"]
        .as_array()
        .is_some_and(Vec::is_empty)
    {
        let mut stored = value.clone();
        stored["allowed_issuers"] = Value::Null;
        let mut policy = policy(&stored);
        policy.allowed_issuers = Some(Vec::new());
        return policy;
    }
    policy(value)
}

fn text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

fn input(case: &Value) -> ResolverInput {
    let raw = &case["input"];
    ResolverInput {
        tenant_default: match &raw["tenant_default"] {
            Value::Null => None,
            value => Some(IdentityPolicyDefault::validate(value).expect("a tenant default")),
        },
        chain: raw["chain"]
            .as_array()
            .expect("a chain")
            .iter()
            .map(|node| ChainNode {
                id: text(&node["id"]).expect("an id"),
                is_ai_system: node["is_ai_system"].as_bool().expect("is_ai_system"),
                lifecycle_stage: text(&node["lifecycle_stage"]),
                risk_tier: text(&node["risk_tier"]),
                identity_policy: match &node["identity_policy"] {
                    Value::Null => None,
                    value => Some(policy(value)),
                },
                gate_exception: node.get("gate_exception").is_some_and(|exception| {
                    exception["requirement"] == "workload_identity_enforced"
                }),
            })
            .collect(),
        attributed_ai_system_id: raw.get("attributed_ai_system_id").and_then(text),
    }
}

#[test]
fn the_fixture_names_this_crates_vocabulary() {
    let fixture = fixture();
    let ladder: Vec<&str> = CredentialAssurance::LADDER
        .iter()
        .map(|level| level.as_str())
        .collect();
    assert_eq!(fixture["ladder"], serde_json::json!(ladder));
    assert_eq!(fixture["policy_fields"], serde_json::json!(POLICY_FIELDS));
    assert_eq!(
        fixture["constants"]["development_default"],
        IdentityPolicy::development_default().to_json()
    );
}

#[test]
fn every_resolver_case() {
    let fixture = fixture();
    let mut failures = Vec::new();
    for case in cases(&fixture, "resolver_cases") {
        let name = case["name"].as_str().expect("a name");
        let expect = &case["expect"];
        let resolved = resolve(&input(case));
        let sources: serde_json::Map<String, Value> = POLICY_FIELDS
            .iter()
            .zip(resolved.sources.iter())
            .map(|(field, source)| ((*field).to_string(), source.to_json()))
            .collect();
        let got = serde_json::json!({
            "effective": resolved.effective.to_json(),
            "sources": sources,
            "inherited_floor": resolved.inherited_floor.to_json(),
            "max_user_key_ttl_days": resolved.max_user_key_ttl_days,
            "put_refused_fields": resolved.put_refused_fields,
        });
        if &got != expect {
            failures.push(format!("{name}:\n  expected {expect}\n  got      {got}"));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn every_judge_case() {
    let fixture = fixture();
    let mut failures = Vec::new();
    for case in cases(&fixture, "judge_cases") {
        let name = case["name"].as_str().expect("a name");
        let c = &case["credential"];
        let idp = text(&c["idp_server_id"]);
        let credential = JudgedCredential {
            kind: CredentialKind::parse(c["kind"].as_str().expect("a kind")).expect("a kind"),
            assurance: c["assurance"]
                .as_str()
                .map(|level| CredentialAssurance::parse(level).expect("a level")),
            is_user_key: c["is_user_key"].as_bool().expect("is_user_key"),
            idp_server_id: idp.as_deref(),
            token_lifetime_s: c["token_lifetime_s"].as_i64(),
        };
        let verdict = judge_credential(&credential, &effective_policy(&case["policy"]));
        let got = serde_json::json!({
            "outcome": verdict.outcome.as_str(),
            "reason": verdict.reason.map(|r| r.as_str()),
            "required": verdict.required.as_str(),
        });
        if got != case["expect"] {
            failures.push(format!("{name}: expected {} got {got}", case["expect"]));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn every_validation_case() {
    let fixture = fixture();
    let mut failures = Vec::new();
    for case in cases(&fixture, "validation_cases") {
        let name = case["name"].as_str().expect("a name");
        let valid = case["valid"].as_bool().expect("valid");
        match (IdentityPolicy::validate(&case["policy"]), valid) {
            (Ok(_), true) => {}
            (Err(invalid), false) if Some(invalid.field.as_str()) == case["field"].as_str() => {}
            (got, _) => failures.push(format!(
                "{name}: expected valid={valid} {} got {got:?}",
                case["field"]
            )),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
