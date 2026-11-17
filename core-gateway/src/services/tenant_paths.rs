// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # What the tenant layer requires of a path
//!
//! The tenant layer (`middleware::tenant`) decides three things from a
//! request's path: whether a tenant is resolved at all, whether the request
//! is refused when none is, and whether it is refused for a tenant that is
//! suspended or whose trial is over. Those used to be three prefix lists
//! written into the middleware, with the paths of other distributions'
//! features among them, and with one default: a path on no list needed no
//! tenant. A route mounted under a prefix of its own silently got no tenant
//! requirement and no suspension block.
//!
//! They are now this registry, held in `AppState.tenant_paths`:
//!
//! * [`TenantPaths::community`] is what the Community Edition serves;
//! * an Enterprise piece adds its paths when it is installed, and an
//!   extension adds its own in `Extension::install_state`
//!   ([`TenantPaths::register`]);
//! * **a path nobody registered gets [`TenantRule::MOST_RESTRICTIVE`]**: a
//!   tenant is required, and a suspended tenant is blocked. Never "no
//!   tenant required".
//!
//! ## Which rule wins
//!
//! The most specific pattern that matches: an exact path before any prefix,
//! then the longer pattern. The order of registration plays no part, and a
//! pattern can be registered once ([`PatternTaken`]), so two pieces cannot
//! disagree about the same pattern depending on which was installed first.

use std::fmt;

/// How a registered rule recognises a path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathPattern {
    /// The path, whole.
    Exact(&'static str),
    /// The path starts with this. A prefix is text, not a segment: write the
    /// trailing `/` when a whole segment is meant.
    Prefix(&'static str),
    /// The path starts with the first and ends with the second.
    PrefixSuffix(&'static str, &'static str),
}

impl PathPattern {
    pub fn matches(&self, path: &str) -> bool {
        match self {
            Self::Exact(exact) => path == *exact,
            Self::Prefix(prefix) => path.starts_with(prefix),
            Self::PrefixSuffix(prefix, suffix) => {
                path.starts_with(prefix) && path.ends_with(suffix)
            }
        }
    }

    /// More specific sorts first: exact, then by the length of what must
    /// match, then by the text (so the order is total and no two patterns
    /// are equal unless they are the same pattern).
    fn rank(&self) -> (u8, std::cmp::Reverse<usize>, &'static str, &'static str) {
        match self {
            Self::Exact(exact) => (0, std::cmp::Reverse(exact.len()), exact, ""),
            Self::Prefix(prefix) => (1, std::cmp::Reverse(prefix.len()), prefix, ""),
            Self::PrefixSuffix(prefix, suffix) => (
                1,
                std::cmp::Reverse(prefix.len() + suffix.len()),
                prefix,
                suffix,
            ),
        }
    }

    fn is_empty(&self) -> bool {
        match self {
            Self::Exact(text) | Self::Prefix(text) => text.is_empty(),
            Self::PrefixSuffix(prefix, _) => prefix.is_empty(),
        }
    }
}

impl fmt::Display for PathPattern {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Exact(exact) => write!(f, "{exact}"),
            Self::Prefix(prefix) => write!(f, "{prefix}*"),
            Self::PrefixSuffix(prefix, suffix) => write!(f, "{prefix}*{suffix}"),
        }
    }
}

/// What the tenant layer does with a request on a path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TenantRule {
    /// No tenant is resolved and none is checked: the path carries its own
    /// tenant (or needs none) and the route decides. For health probes,
    /// public discovery documents and protocols that put the tenant in the
    /// path.
    Open,
    /// A tenant is resolved from the request when it names one. An unknown
    /// tenant is refused and an unreadable status is "unavailable", always.
    Resolve {
        /// Refuse (401) a request that names no tenant.
        tenant_required: bool,
        /// Refuse (403) a tenant that is suspended or whose trial is over.
        blocked_when_suspended: bool,
    },
}

impl TenantRule {
    /// What a path nobody registered gets: a tenant is required, an unknown
    /// tenant is refused, a suspended tenant is blocked.
    pub const MOST_RESTRICTIVE: Self = Self::Resolve {
        tenant_required: true,
        blocked_when_suspended: true,
    };

    /// A tenant is resolved when the request names one; nothing is refused
    /// for want of one, and a suspended tenant is not blocked (the route
    /// authenticates its caller itself).
    pub const TENANT_OPTIONAL: Self = Self::Resolve {
        tenant_required: false,
        blocked_when_suspended: false,
    };

    /// Runtime traffic that may arrive without a tenant header (the route
    /// resolves its caller) but is blocked for a suspended tenant.
    pub const RUNTIME: Self = Self::Resolve {
        tenant_required: false,
        blocked_when_suspended: true,
    };

    pub fn skips_resolution(self) -> bool {
        self == Self::Open
    }

    pub fn requires_tenant(self) -> bool {
        matches!(
            self,
            Self::Resolve {
                tenant_required: true,
                ..
            }
        )
    }

    pub fn blocks_suspended(self) -> bool {
        matches!(
            self,
            Self::Resolve {
                blocked_when_suspended: true,
                ..
            }
        )
    }
}

/// A pattern somebody already registered.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PatternTaken(pub PathPattern);

impl fmt::Display for PatternTaken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "the tenant layer already has a rule for the path pattern {}",
            self.0
        )
    }
}

impl std::error::Error for PatternTaken {}

/// The registry. Rules are kept most specific first, so the first match is
/// the one that wins.
#[derive(Clone, Debug)]
pub struct TenantPaths {
    rules: Vec<(PathPattern, TenantRule)>,
}

/// The paths of the Community Edition.
const COMMUNITY: &[(PathPattern, TenantRule)] = &[
    // Infrastructure: health and version probes carry nothing.
    (PathPattern::Exact("/health"), TenantRule::Open),
    (PathPattern::Exact("/healthz"), TenantRule::Open),
    (PathPattern::Exact("/v1/version"), TenantRule::Open),
    (PathPattern::Exact("/v1/auth/info"), TenantRule::Open),
    // OAuth proxy: discovery and token exchange resolve their context from
    // path parameters, not from tenant headers or a token (RFC 8414).
    (PathPattern::Prefix("/v1/proxy/oauth/"), TenantRule::Open),
    // A2A public Agent Card and JWK Set discovery: the tenant is in the
    // path; a reader has no credentials yet.
    (
        PathPattern::Prefix("/.well-known/agent-card/"),
        TenantRule::Open,
    ),
    (
        PathPattern::Prefix("/.well-known/a2a-jwks/"),
        TenantRule::Open,
    ),
    // A2A v1.0 HTTP/REST: `/a2a/{tenant_id}/{card_name}/...`; the route
    // authenticates and checks the tenant's status itself.
    (PathPattern::Prefix("/a2a/"), TenantRule::Open),
    // The proxy surfaces and the Anthropic-style messages API: a tenant is
    // required and a suspended tenant is blocked.
    (
        PathPattern::Prefix("/v1/proxy/"),
        TenantRule::MOST_RESTRICTIVE,
    ),
    (
        PathPattern::Prefix("/v1/messages"),
        TenantRule::MOST_RESTRICTIVE,
    ),
    // Google's native Gemini surface, merged at the root under its own
    // version prefix: billable runtime traffic like every provider call.
    (PathPattern::Prefix("/v1beta/"), TenantRule::RUNTIME),
    // Tool approvals: an agent polling its parked call, and a
    // decision on it, are runtime traffic; a suspended tenant's agents must
    // not learn a decision. The list route itself (`/v1/approvals`, no
    // trailing segment) was never under the block.
    (PathPattern::Prefix("/v1/approvals/"), TenantRule::RUNTIME),
    (
        PathPattern::Exact("/v1/approvals"),
        TenantRule::TENANT_OPTIONAL,
    ),
    // Runs: closing and reading a run was never blocked for a suspended
    // tenant; rating one is runtime traffic.
    (
        PathPattern::Prefix("/v1/runs/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    (
        PathPattern::PrefixSuffix("/v1/runs/", "/feedback"),
        TenantRule::RUNTIME,
    ),
    // Called by the control plane or a relying party with a credential of
    // their own; each route authenticates its caller.
    (
        PathPattern::Prefix("/v1/audit/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    (
        PathPattern::Prefix("/v1/evidence/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    (
        PathPattern::Exact("/.well-known/brutor-evidence-keys.json"),
        TenantRule::TENANT_OPTIONAL,
    ),
    (
        PathPattern::Prefix("/v1/ingest/otel/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    (
        PathPattern::Prefix("/v1/assurance/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    // The control plane decides an approval on an administrator's behalf,
    // for whichever tenant: not keyed on the caller's tenant.
    (
        PathPattern::Prefix("/v1/internal/approvals/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    (
        PathPattern::Prefix("/v1/assets/"),
        TenantRule::TENANT_OPTIONAL,
    ),
    // The filesystem media store's signed download links: the tenant is the
    // key's first segment and the signature covers it; the route checks it.
    (PathPattern::Prefix("/v1/media/"), TenantRule::Open),
    // The API document and its viewer.
    (PathPattern::Exact("/docs"), TenantRule::TENANT_OPTIONAL),
    (PathPattern::Prefix("/docs/"), TenantRule::TENANT_OPTIONAL),
    (
        PathPattern::Exact("/api-docs/openapi.json"),
        TenantRule::TENANT_OPTIONAL,
    ),
];

impl TenantPaths {
    /// The Community Edition's paths: what `AppState::new` holds.
    pub fn community() -> Self {
        let mut paths = Self { rules: Vec::new() };
        for (pattern, rule) in COMMUNITY {
            // The table above names no pattern twice (a unit test holds it
            // to that); a duplicate would be dropped here, never override.
            let _ = paths.register(*pattern, *rule);
        }
        paths
    }

    /// Add a rule. Refused when the pattern is already registered (by the
    /// Community Edition, an Enterprise piece or an extension), whatever its
    /// rule: nothing overrides a rule that is in place.
    pub fn register(&mut self, pattern: PathPattern, rule: TenantRule) -> Result<(), PatternTaken> {
        if pattern.is_empty() || self.rules.iter().any(|(p, _)| *p == pattern) {
            return Err(PatternTaken(pattern));
        }
        let at = self
            .rules
            .partition_point(|(p, _)| p.rank() < pattern.rank());
        self.rules.insert(at, (pattern, rule));
        Ok(())
    }

    /// The rule somebody registered for this path, with its pattern; `None`
    /// when nobody did.
    pub fn registered(&self, path: &str) -> Option<(PathPattern, TenantRule)> {
        self.rules
            .iter()
            .find(|(pattern, _)| pattern.matches(path))
            .copied()
    }

    /// The rule for this path: the most specific registered one, or
    /// [`TenantRule::MOST_RESTRICTIVE`] when nobody registered the path.
    pub fn rule_for(&self, path: &str) -> TenantRule {
        match self.registered(path) {
            Some((_, rule)) => rule,
            None => TenantRule::MOST_RESTRICTIVE,
        }
    }

    /// Every rule, most specific first.
    pub fn rules(&self) -> &[(PathPattern, TenantRule)] {
        &self.rules
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(path: &str) -> TenantRule {
        TenantPaths::community().rule_for(path)
    }

    // The three questions the middleware used to answer with three lists.
    fn skips(path: &str) -> bool {
        rule(path).skips_resolution()
    }
    fn requires(path: &str) -> bool {
        rule(path).requires_tenant()
    }
    fn runtime(path: &str) -> bool {
        rule(path).blocks_suspended()
    }

    // -- a path nobody registered ---------------------------------------------

    #[test]
    fn an_unknown_path_gets_the_most_restrictive_rule() {
        for path in [
            "/",
            "/v1/unknown",
            "/v1/ext/reports",
            "/v2/anything",
            "/v1/proxy",
            "/v1/approvals-report",
            "/health/deep",
            "",
        ] {
            let paths = TenantPaths::community();
            assert_eq!(paths.registered(path), None, "{path}");
            let rule = paths.rule_for(path);
            assert_eq!(rule, TenantRule::MOST_RESTRICTIVE, "{path}");
            assert!(rule.requires_tenant(), "{path}: never `no tenant required`");
            assert!(rule.blocks_suspended(), "{path}");
            assert!(!rule.skips_resolution(), "{path}");
        }
    }

    #[test]
    fn the_most_restrictive_rule_is_the_strictest_of_all() {
        let every = [
            TenantRule::Open,
            TenantRule::TENANT_OPTIONAL,
            TenantRule::RUNTIME,
            TenantRule::Resolve {
                tenant_required: true,
                blocked_when_suspended: false,
            },
            TenantRule::MOST_RESTRICTIVE,
        ];
        let strict = TenantRule::MOST_RESTRICTIVE;
        for rule in every {
            assert!(strict.requires_tenant() || !rule.requires_tenant());
            assert!(strict.blocks_suspended() || !rule.blocks_suspended());
            assert!(!strict.skips_resolution());
        }
    }

    // -- which rule wins ------------------------------------------------------

    #[test]
    fn the_most_specific_pattern_wins_whatever_the_order_of_registration() {
        let entries = [
            (PathPattern::Prefix("/x/"), TenantRule::MOST_RESTRICTIVE),
            (PathPattern::Prefix("/x/open/"), TenantRule::Open),
            (
                PathPattern::PrefixSuffix("/x/", "/rate"),
                TenantRule::RUNTIME,
            ),
            (
                PathPattern::Exact("/x/open/but-not-this"),
                TenantRule::TENANT_OPTIONAL,
            ),
            (PathPattern::Prefix("/x"), TenantRule::TENANT_OPTIONAL),
        ];
        let probes = [
            ("/x/a", TenantRule::MOST_RESTRICTIVE),
            ("/x/open/a", TenantRule::Open),
            ("/x/open/but-not-this", TenantRule::TENANT_OPTIONAL),
            ("/x/1/rate", TenantRule::RUNTIME),
            ("/xy", TenantRule::TENANT_OPTIONAL),
            ("/y", TenantRule::MOST_RESTRICTIVE),
        ];
        // Every order of registration answers the same.
        let mut order: Vec<usize> = (0..entries.len()).collect();
        let mut orders = 0;
        permute(&mut order, 0, &mut |order| {
            let mut paths = TenantPaths { rules: Vec::new() };
            for i in order {
                paths.register(entries[*i].0, entries[*i].1).expect("once");
            }
            for (path, expected) in probes {
                assert_eq!(paths.rule_for(path), expected, "{path} in order {order:?}");
            }
            orders += 1;
        });
        assert_eq!(orders, 120);
    }

    fn permute(items: &mut Vec<usize>, from: usize, visit: &mut dyn FnMut(&[usize])) {
        if from == items.len() {
            visit(items);
            return;
        }
        for i in from..items.len() {
            items.swap(from, i);
            permute(items, from + 1, visit);
            items.swap(from, i);
        }
    }

    #[test]
    fn a_pattern_is_registered_once() {
        let mut paths = TenantPaths::community();
        let before = paths.rules().len();
        // Not to loosen a rule that is in place …
        assert_eq!(
            paths.register(PathPattern::Prefix("/v1/proxy/"), TenantRule::Open),
            Err(PatternTaken(PathPattern::Prefix("/v1/proxy/")))
        );
        // … nor to restate it.
        assert!(
            paths
                .register(PathPattern::Exact("/health"), TenantRule::Open)
                .is_err()
        );
        // An empty pattern would be a rule for every path.
        assert!(
            paths
                .register(PathPattern::Prefix(""), TenantRule::Open)
                .is_err()
        );
        assert_eq!(paths.rules().len(), before);
        assert!(requires("/v1/proxy/llm/chat/completions"));
        // A new one is added, and is then taken.
        paths
            .register(PathPattern::Prefix("/v1/ext/"), TenantRule::RUNTIME)
            .expect("a new pattern");
        assert_eq!(paths.rule_for("/v1/ext/reports"), TenantRule::RUNTIME);
        assert!(
            paths
                .register(PathPattern::Prefix("/v1/ext/"), TenantRule::RUNTIME)
                .is_err()
        );
    }

    #[test]
    fn the_community_table_names_no_pattern_twice() {
        let paths = TenantPaths::community();
        assert_eq!(paths.rules().len(), COMMUNITY.len());
        // Kept most specific first.
        for pair in paths.rules().windows(2) {
            assert!(pair[0].0.rank() < pair[1].0.rank(), "{pair:?}");
        }
    }

    // -- blocked for a suspended tenant ---------------------------------------
    // (were `middleware::tenant::tests::runtime_path_*`)

    #[test]
    fn runtime_path_proxy() {
        assert!(runtime("/v1/proxy/llm/chat/completions"));
    }

    #[test]
    fn runtime_path_messages() {
        assert!(runtime("/v1/messages"));
    }

    #[test]
    fn runtime_path_approvals() {
        // Tool approvals have a prefix of their own. An agent
        // polling its parked call, and a decision on it, are runtime traffic
        // for a suspended tenant exactly as they were under the old prefix.
        assert!(runtime("/v1/approvals/01J.../poll"));
        assert!(runtime("/v1/approvals/01J.../approve"));
        assert!(runtime("/v1/approvals/pending-count"));
        // the prefix, not a word: this one is a path nobody registered, and
        // so not "runtime" by the approvals rule but by the default
        assert_eq!(
            TenantPaths::community().registered("/v1/approvals-report"),
            None
        );
        // the list route itself was never blocked
        assert_eq!(rule("/v1/approvals"), TenantRule::TENANT_OPTIONAL);
    }

    #[test]
    fn runtime_path_run_feedback_but_not_run_end() {
        // The rating route moved under /v1/runs and stays
        // runtime traffic; the lifecycle routes there are unchanged.
        assert!(runtime("/v1/runs/root-1/feedback"));
        assert!(!runtime("/v1/runs/root-1/end"));
        assert!(!runtime("/v1/runs/root-1"));
    }

    #[test]
    fn runtime_path_gemini_native() {
        // Suspended/expired tenants must be blocked on Google's native
        // `/v1beta` surface too: it is not under `/v1/proxy/`.
        assert!(runtime("/v1beta/models/gemini-2.5-pro:generateContent"));
        assert!(runtime(
            "/v1beta/models/gemini-2.5-pro:streamGenerateContent"
        ));
    }

    #[test]
    fn not_runtime_path_health() {
        assert!(!runtime("/health"));
    }

    // -- no tenant resolution -------------------------------------------------
    // (were `middleware::tenant::tests::skip_*` / `no_skip_*`)

    #[test]
    fn skip_health_and_version_endpoints() {
        assert!(skips("/health"));
        assert!(skips("/healthz"));
        assert!(skips("/v1/version"));
        assert!(skips("/v1/auth/info"));
    }

    #[test]
    fn skip_oauth_proxy_paths() {
        assert!(skips("/v1/proxy/oauth/callback"));
        assert!(skips("/v1/proxy/oauth/.well-known/openid-configuration"));
        assert!(skips("/v1/proxy/oauth/token/server-123/idp-456"));
    }

    #[test]
    fn skip_a2a_v1_paths() {
        // Spec v1.0 method routes carry the tenant in the path.
        assert!(skips("/a2a/tenant-abc/my-agent/message:send"));
        assert!(skips("/a2a/tenant-abc/my-agent/tasks/task-xyz:cancel"));
        assert!(skips(
            "/a2a/tenant-abc/my-agent/tasks/task-xyz/pushNotificationConfigs"
        ));
    }

    #[test]
    fn skip_a2a_discovery_paths() {
        // Public by definition: the tenant is in the path.
        assert!(skips("/.well-known/a2a-jwks/tenant-abc"));
        assert!(skips("/.well-known/agent-card/tenant-abc/my-agent"));
    }

    #[test]
    fn no_skip_proxy_paths() {
        assert!(!skips("/v1/proxy/llm/chat/completions"));
        assert!(!skips("/v1/proxy/mcp/server-123"));
        assert!(!skips("/v1/messages"));
    }

    #[test]
    fn no_skip_root_or_arbitrary_path() {
        assert!(!skips("/"));
        assert!(!skips("/v1/admin/tenants"));
    }

    // -- a tenant is required -------------------------------------------------
    // (were `middleware::tenant::tests::requires_tenant_*`)

    #[test]
    fn requires_tenant_for_proxy_routes() {
        assert!(requires("/v1/proxy/llm/chat/completions"));
        assert!(requires("/v1/proxy/mcp/server-123"));
    }

    #[test]
    fn requires_tenant_for_messages_routes() {
        assert!(requires("/v1/messages"));
        assert!(requires("/v1/messages/stream"));
    }

    #[test]
    fn no_tenant_required_where_the_route_resolves_its_caller() {
        assert!(!requires("/health"));
        assert!(!requires("/v1/audit/chains"));
        assert!(!requires("/v1/runs/root-1/end"));
        assert!(!requires("/v1/approvals/01J.../poll"));
    }

    #[test]
    fn a_path_nobody_serves_requires_a_tenant() {
        // Before the registry these were "no tenant required" (they were on
        // no list). They are on no list now either, and that means the
        // opposite.
        assert!(requires("/v1/admin/tenants"));
        assert!(requires("/"));
        assert!(runtime("/v1/admin/tenants"));
    }
}
