// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Outbound destination policy — the SSRF guard for tenant-configured
//! upstream URLs (MCP servers, LLM provider endpoints, A2A agents, IdP
//! discovery, MCP App bundles).
//!
//! Tenant admins configure URLs the proxy then fetches with the proxy's
//! own network position and credentials. Without a policy a tenant could
//! point an "MCP server" at the cloud metadata service or at an internal
//! service and read the answer. Two layers, one rule set:
//!
//! * **DNS resolution** — every outbound client built through
//!   [`client_builder`] resolves hostnames with [`PolicyResolver`], which
//!   drops denied addresses *after* resolution, so rebinding a public
//!   name to a private address does not help either.
//! * **URL check** — hyper never resolves an IP-literal host, so URLs
//!   are also checked with [`check_url`] where they enter the request
//!   path (MCP server resolution, A2A remote dispatch), and on every
//!   redirect hop by the redirect policy [`client_builder`] installs.
//!
//! What is denied:
//!
//! * always, whatever the allow-list says: link-local (`169.254/16`,
//!   `fe80::/10`) — where every cloud metadata service lives — the IPv6
//!   metadata address `fd00:ec2::254`, "this network" (`0/8`), the
//!   unspecified and broadcast addresses and multicast;
//! * by default: loopback, RFC 1918, carrier-grade NAT (`100.64/10`),
//!   IPv6 unique-local (`fc00::/7`) and site-local (`fec0::/10`): the
//!   deployment's own network (its database, Redis, object store), which a
//!   URL a tenant admin enters must not reach (SSRF). The operator allows
//!   an internal destination by listing it in `BRUTOR_OUTBOUND_ALLOW_HOSTS`:
//!   a host name, `*.domain` (the domain and its subdomains), an IP address
//!   or a CIDR range. An entry it cannot read refuses the start.
//!
//! IPv4 carried in IPv6 (mapped, compatible, NAT64 `64:ff9b::/96`, 6to4
//! `2002::/16`) is judged as the IPv4 address it carries. The semantics are
//! shared with the Control Plane and KB Connector Sync.
//!
//! First-party clients that only ever talk to operator-configured
//! services (the skill runner, the reranker) are deliberately built
//! without the policy: an operator's own private endpoint is not a
//! tenant-controlled destination.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::sync::{Arc, OnceLock};

use reqwest::dns::{Addrs, Name, Resolve, Resolving};

/// A destination the policy refused, with the reason a log or error
/// message should carry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Denied {
    pub host: String,
    pub reason: &'static str,
}

impl std::fmt::Display for Denied {
    /// Names the destination host (never the whole URL, which may carry a
    /// credential) and the setting that decides it.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "outbound destination '{}' refused by policy: {}",
            self.host, self.reason
        )?;
        if ALWAYS_DENIED_REASONS.contains(&self.reason) {
            write!(
                f,
                ". This range is never allowed, whatever {ALLOW_SETTING} says"
            )
        } else if DEFAULT_DENIED_REASONS.contains(&self.reason) {
            write!(
                f,
                ". To allow an internal destination, add it to {ALLOW_SETTING} (a host name, \
                 *.domain, IP address or CIDR range)"
            )
        } else {
            Ok(())
        }
    }
}

/// The setting that lists the destinations allowed besides public ones.
const ALLOW_SETTING: &str = "BRUTOR_OUTBOUND_ALLOW_HOSTS";

const REASON_RESERVED: &str = "reserved address";
const REASON_LINK_LOCAL: &str = "link-local address (cloud metadata range)";
const REASON_METADATA: &str = "cloud metadata address";
const REASON_LOOPBACK: &str = "loopback address";
const REASON_RFC1918: &str = "private (RFC 1918) address";
const REASON_CGNAT: &str = "carrier-grade NAT address";
const REASON_ULA: &str = "unique-local (private) address";
const REASON_SITE_LOCAL: &str = "site-local (private) address";

/// Reasons no allow-list entry lifts.
const ALWAYS_DENIED_REASONS: &[&str] = &[REASON_RESERVED, REASON_LINK_LOCAL, REASON_METADATA];
/// Reasons a matching allow-list entry lifts.
const DEFAULT_DENIED_REASONS: &[&str] = &[
    REASON_LOOPBACK,
    REASON_RFC1918,
    REASON_CGNAT,
    REASON_ULA,
    REASON_SITE_LOCAL,
];

/// An IP network: `addr/prefix` (an address alone is `/32` or `/128`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Network {
    addr: IpAddr,
    prefix: u8,
}

impl Network {
    /// `10.0.0.0/8`, `fd00::/8`, `10.0.0.5`, `[fd12::1]`.
    pub fn parse(raw: &str) -> Option<Self> {
        let (addr, prefix) = match raw.split_once('/') {
            Some((addr, prefix)) => (addr, Some(prefix)),
            None => (raw, None),
        };
        let addr = addr.trim_start_matches('[').trim_end_matches(']');
        let addr: IpAddr = addr.parse().ok()?;
        let max = if addr.is_ipv4() { 32 } else { 128 };
        let prefix = match prefix {
            Some(p) if !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()) => p.parse().ok()?,
            Some(_) => return None,
            None => max,
        };
        (prefix <= max).then_some(Self { addr, prefix })
    }

    /// Whether `ip` is in this network. IPv4 carried in IPv6 matches as
    /// the IPv4 address it carries.
    pub fn contains(&self, ip: IpAddr) -> bool {
        let ip = canonical(ip);
        match (canonical(self.addr), ip) {
            (IpAddr::V4(net), IpAddr::V4(ip)) => {
                let mask = u32::MAX
                    .checked_shl(32 - u32::from(self.prefix))
                    .unwrap_or(0);
                u32::from(net) & mask == u32::from(ip) & mask
            }
            (IpAddr::V6(net), IpAddr::V6(ip)) => {
                let mask = u128::MAX
                    .checked_shl(128 - u32::from(self.prefix))
                    .unwrap_or(0);
                u128::from(net) & mask == u128::from(ip) & mask
            }
            _ => false,
        }
    }
}

/// The IPv4 address an IPv6 address carries (mapped `::ffff:a.b.c.d`,
/// compatible `::a.b.c.d`, NAT64 `64:ff9b::a.b.c.d`, 6to4
/// `2002:AABB:CCDD::/48`), or the address itself.
fn canonical(ip: IpAddr) -> IpAddr {
    let IpAddr::V6(v6) = ip else { return ip };
    if let Some(v4) = v6.to_ipv4_mapped() {
        return IpAddr::V4(v4);
    }
    let seg = v6.segments();
    let low = Ipv4Addr::new(
        (seg[6] >> 8) as u8,
        seg[6] as u8,
        (seg[7] >> 8) as u8,
        seg[7] as u8,
    );
    // IPv4-compatible (deprecated), but not `::` and `::1`.
    if seg[..6] == [0; 6] && !(seg[6] == 0 && seg[7] <= 1) {
        return IpAddr::V4(low);
    }
    // NAT64 well-known prefix.
    if seg[..6] == [0x64, 0xff9b, 0, 0, 0, 0] {
        return IpAddr::V4(low);
    }
    // 6to4.
    if seg[0] == 0x2002 {
        return IpAddr::V4(Ipv4Addr::new(
            (seg[1] >> 8) as u8,
            seg[1] as u8,
            (seg[2] >> 8) as u8,
            seg[2] as u8,
        ));
    }
    ip
}

impl std::error::Error for Denied {}

/// The outbound policy: public destinations, plus what the allow-list
/// names. [`Default`] is the policy with an empty allow-list: every private
/// destination is refused.
#[derive(Debug, Clone, Default)]
pub struct OutboundPolicy {
    /// Host names (lower-case) whose every resolved address is allowed but
    /// the always-denied ones; `*.example.internal` also matches
    /// `example.internal` and its subdomains. `BRUTOR_OUTBOUND_ALLOW_HOSTS`.
    pub allow_hosts: Vec<String>,
    /// IP addresses and CIDR ranges allowed besides the public ones (never
    /// an always-denied address). `BRUTOR_OUTBOUND_ALLOW_HOSTS`.
    pub allow_networks: Vec<Network>,
}

/// Whether `name` is a host name or `*.domain` entry the allow-list
/// accepts: letters, digits, `-`, `_` and `.`, with `*` only as a leading
/// `*.`.
fn is_host_entry(name: &str) -> bool {
    let bare = name.strip_prefix("*.").unwrap_or(name);
    !bare.is_empty()
        && !bare.starts_with('.')
        && bare
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
}

impl OutboundPolicy {
    /// A policy that allows `entries` (host names, `*.domain`, IP
    /// addresses, CIDR ranges) besides public destinations. `Err`: an entry
    /// it cannot read.
    pub fn with_allowed<S: AsRef<str>>(entries: &[S]) -> Result<Self, String> {
        let mut policy = Self::default();
        for entry in entries {
            let entry = entry
                .as_ref()
                .trim()
                .trim_end_matches('.')
                .to_ascii_lowercase();
            if entry.is_empty() {
                continue;
            }
            if let Some(network) = Network::parse(&entry) {
                policy.allow_networks.push(network);
            } else if is_host_entry(&entry) {
                policy.allow_hosts.push(entry);
            } else {
                return Err(entry);
            }
        }
        Ok(policy)
    }

    /// The policy `env` configures. An allow-list entry it cannot read is
    /// refused (`config::check` stops the start on it), naming the
    /// variable and never the value.
    pub fn try_from_env(
        env: &crate::env_registry::Env,
    ) -> Result<Self, crate::env_registry::EnvError> {
        let var = &crate::env_vars::BRUTOR_OUTBOUND_ALLOW_HOSTS;
        Self::with_allowed(&env.list(var)).map_err(|_| crate::env_vars::invalid(var, var.accepted))
    }

    /// [`OutboundPolicy::try_from_env`]; a list the start would have
    /// refused allows nothing (fail closed).
    pub fn from_env(env: &crate::env_registry::Env) -> Self {
        Self::try_from_env(env).unwrap_or_default()
    }

    /// Whether `host` (a host name) is on the allow-list by name.
    pub fn host_allowed(&self, host: &str) -> bool {
        let h = host.trim_end_matches('.').to_ascii_lowercase();
        self.allow_hosts.iter().any(|entry| {
            if let Some(suffix) = entry.strip_prefix("*.") {
                h == suffix || h.ends_with(&format!(".{suffix}"))
            } else {
                h == *entry
            }
        })
    }

    /// Refuse an address the policy does not permit.
    pub fn check_ip(&self, ip: IpAddr) -> Result<(), &'static str> {
        always_denied(ip)?;
        match default_denied(ip) {
            Err(reason) if !self.allow_networks.iter().any(|n| n.contains(ip)) => Err(reason),
            _ => Ok(()),
        }
    }

    /// [`OutboundPolicy::check_ip`] for an address a host name on the
    /// allow-list resolved to: only the always-denied ranges are refused.
    fn check_ip_of_allowed_host(&self, ip: IpAddr) -> Result<(), &'static str> {
        always_denied(ip)
    }

    /// Check a URL before it is used: scheme must be http(s); an IP-literal
    /// host is checked against the policy; a hostname is left to
    /// [`PolicyResolver`] (which also judges an allow-listed name's
    /// addresses).
    pub fn check_url(&self, url: &str) -> Result<(), Denied> {
        let parsed = reqwest::Url::parse(url.trim()).map_err(|_| Denied {
            host: url.to_string(),
            reason: "not a valid URL",
        })?;
        if !matches!(parsed.scheme(), "http" | "https") {
            return Err(Denied {
                host: url.to_string(),
                reason: "only http and https destinations are allowed",
            });
        }
        let Some(host) = parsed.host_str() else {
            return Err(Denied {
                host: url.to_string(),
                reason: "URL has no host",
            });
        };
        let literal = host.trim_start_matches('[').trim_end_matches(']');
        if let Ok(ip) = literal.parse::<IpAddr>() {
            self.check_ip(ip).map_err(|reason| Denied {
                host: host.to_string(),
                reason,
            })?;
        }
        Ok(())
    }
}

/// Refused whatever the allow-list says.
fn always_denied(ip: IpAddr) -> Result<(), &'static str> {
    match canonical(ip) {
        IpAddr::V4(v4) => {
            if v4.octets()[0] == 0 || v4.is_broadcast() || v4.is_multicast() {
                return Err(REASON_RESERVED);
            }
            if v4.is_link_local() {
                return Err(REASON_LINK_LOCAL);
            }
        }
        IpAddr::V6(v6) => {
            if v6.is_unspecified() || v6.is_multicast() {
                return Err(REASON_RESERVED);
            }
            if (v6.segments()[0] & 0xffc0) == 0xfe80 {
                return Err(REASON_LINK_LOCAL);
            }
            // fd00:ec2::254 — the IPv6 instance metadata address.
            if v6 == Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x254) {
                return Err(REASON_METADATA);
            }
        }
    }
    Ok(())
}

/// Refused unless the allow-list names it.
fn default_denied(ip: IpAddr) -> Result<(), &'static str> {
    match canonical(ip) {
        IpAddr::V4(v4) => {
            if v4.is_loopback() {
                return Err(REASON_LOOPBACK);
            }
            if v4.is_private() {
                return Err(REASON_RFC1918);
            }
            let o = v4.octets();
            if o[0] == 100 && (64..=127).contains(&o[1]) {
                return Err(REASON_CGNAT);
            }
        }
        IpAddr::V6(v6) => {
            let seg = v6.segments();
            if v6.is_loopback() {
                return Err(REASON_LOOPBACK);
            }
            if (seg[0] & 0xfe00) == 0xfc00 {
                return Err(REASON_ULA);
            }
            if (seg[0] & 0xffc0) == 0xfec0 {
                return Err(REASON_SITE_LOCAL);
            }
        }
    }
    Ok(())
}

/// [`check_url`] for an MCP server's `base_url`, which every path that
/// loads a server row checks before sending. Built-in system servers
/// (`http://system/…`) are served in-process and never fetched.
pub fn check_mcp_server_url(base_url: &str) -> Result<(), Denied> {
    if crate::constants::system_server::is_system_server(base_url) {
        return Ok(());
    }
    check_url(base_url).inspect_err(|denied| {
        tracing::warn!(%denied, "MCP server refused by outbound policy");
    })
}

/// The process-wide policy, read once.
pub fn policy() -> &'static OutboundPolicy {
    static POLICY: OnceLock<OutboundPolicy> = OnceLock::new();
    POLICY.get_or_init(|| OutboundPolicy::from_env(&crate::env_vars::process()))
}

/// [`OutboundPolicy::check_url`] against the process-wide policy.
pub fn check_url(url: &str) -> Result<(), Denied> {
    policy().check_url(url)
}

/// A `reqwest` DNS resolver that drops addresses the policy denies.
#[derive(Debug, Clone)]
pub struct PolicyResolver {
    policy: Arc<OutboundPolicy>,
}

impl PolicyResolver {
    pub fn new(policy: OutboundPolicy) -> Self {
        Self {
            policy: Arc::new(policy),
        }
    }
}

impl Resolve for PolicyResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let policy = self.policy.clone();
        Box::pin(async move {
            let host = name.as_str().to_string();
            let resolved: Vec<SocketAddr> = tokio::net::lookup_host((host.as_str(), 0))
                .await
                .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)?
                .collect();
            // The connection is made to these addresses, checked here, and to
            // no other: a name re-pointed at a private address after an
            // earlier check (DNS rebinding) is judged again on every connect.
            let by_name = policy.host_allowed(&host);
            let mut denied_reason = None;
            let allowed: Vec<SocketAddr> = resolved
                .into_iter()
                .filter(|addr| {
                    let verdict = if by_name {
                        policy.check_ip_of_allowed_host(addr.ip())
                    } else {
                        policy.check_ip(addr.ip())
                    };
                    match verdict {
                        Ok(()) => true,
                        Err(reason) => {
                            denied_reason = Some(reason);
                            false
                        }
                    }
                })
                .collect();
            if allowed.is_empty() {
                if let Some(reason) = denied_reason {
                    tracing::warn!(host = %host, reason, "outbound destination refused by policy");
                    return Err(Box::new(Denied { host, reason })
                        as Box<dyn std::error::Error + Send + Sync>);
                }
            }
            Ok(Box::new(allowed.into_iter()) as Addrs)
        })
    }
}

/// Most redirect hops a policy client follows.
pub const MAX_REDIRECTS: usize = 5;

/// Whether a redirect may leave the origin (scheme, host, port) of the
/// request it answers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrossOrigin {
    /// Refuse it. For every client that sends credentials: reqwest drops
    /// `Authorization`, `Cookie` and `Proxy-Authorization` on a cross-host
    /// hop, but forwards every other header, so a provider key in
    /// `x-api-key` / `api-key` / `x-goog-api-key` (or an MCP server's
    /// custom auth header) would reach whatever host the redirect names.
    Refuse,
    /// Follow it. Only for [`public_fetch_client`], which never carries
    /// credentials.
    Follow,
}

/// Same scheme, host and (effective) port.
fn same_origin(a: &reqwest::Url, b: &reqwest::Url) -> bool {
    a.scheme() == b.scheme()
        && a.host_str().map(str::to_ascii_lowercase) == b.host_str().map(str::to_ascii_lowercase)
        && a.port_or_known_default() == b.port_or_known_default()
}

/// A redirect policy that checks every hop's URL against the policy. The
/// resolver already covers a hostname target on the next connect, but an
/// IP-literal target never reaches it: without this an upstream could
/// answer `302 Location: http://169.254.169.254/…` and the proxy would
/// fetch it. Under [`CrossOrigin::Refuse`] a hop to another origin
/// is refused as well.
pub fn redirect_policy(
    policy: Arc<OutboundPolicy>,
    max: usize,
    cross_origin: CrossOrigin,
) -> reqwest::redirect::Policy {
    reqwest::redirect::Policy::custom(move |attempt| {
        // `previous()` includes the original URL, so it holds `max` entries
        // when the `max`-th redirect is being asked for (as `limited`).
        if attempt.previous().len() > max {
            return attempt.error(format!("too many redirects (more than {max})"));
        }
        if let Err(denied) = policy.check_url(attempt.url().as_str()) {
            tracing::warn!(host = %denied.host, reason = denied.reason, "outbound redirect refused by policy");
            return attempt.error(denied);
        }
        if cross_origin == CrossOrigin::Refuse
            && let Some(from) = attempt.previous().last()
            && !same_origin(from, attempt.url())
        {
            let to = attempt.url().origin().ascii_serialization();
            tracing::warn!(from = %from.origin().ascii_serialization(), to = %to, "cross-origin redirect refused on a credentialed client");
            return attempt.error(format!(
                "cross-origin redirect to {to} refused: this client may carry credentials (outbound policy)"
            ));
        }
        attempt.follow()
    })
}

fn builder(policy: OutboundPolicy, cross_origin: CrossOrigin) -> reqwest::ClientBuilder {
    let policy = Arc::new(policy);
    reqwest::Client::builder()
        .dns_resolver(Arc::new(PolicyResolver {
            policy: policy.clone(),
        }))
        .redirect(redirect_policy(policy, MAX_REDIRECTS, cross_origin))
}

/// A `reqwest::ClientBuilder` that enforces `policy` on DNS resolution and
/// on every redirect hop, and follows redirects within the same origin
/// only. A caller must not set `.redirect(…)` afterwards (that replaces the
/// checked policy; `tests/outbound_client_guard.rs` enforces it), except
/// `Policy::none()`.
pub fn client_builder_with(policy: OutboundPolicy) -> reqwest::ClientBuilder {
    builder(policy, CrossOrigin::Refuse)
}

/// [`client_builder_with`] the process-wide policy. Every client that
/// fetches a tenant-controlled destination is built through this.
pub fn client_builder() -> reqwest::ClientBuilder {
    client_builder_with(policy().clone())
}

/// [`client_builder_with`], except that redirects may change origin. Only
/// for [`public_fetch_client`].
pub fn public_fetch_builder_with(policy: OutboundPolicy) -> reqwest::ClientBuilder {
    builder(policy, CrossOrigin::Follow)
}

/// The client for fetching public URLs with NO credentials: a web page the
/// search tool extracts, an image or video URL a provider answered with.
/// Those legitimately redirect across origins (http to https, apex to
/// www, to a CDN). Never attach an API key, token or auth header to a
/// request on it: use the shared credentialed client for that.
/// `tests/outbound_client_guard.rs` limits where it is used.
pub fn public_fetch_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        public_fetch_builder_with(policy().clone())
            .connect_timeout(std::time::Duration::from_secs(10))
            .timeout(std::time::Duration::from_secs(120))
            .build()
            // Building a client only fails if the TLS backend cannot
            // initialise, which the shared client at startup would
            // already have hit.
            .expect("public fetch client")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// What the tests' upstream mocks need: loopback allowed.
    fn open() -> OutboundPolicy {
        OutboundPolicy::with_allowed(&["localhost", "127.0.0.0/8", "::1"]).unwrap()
    }

    fn strict() -> OutboundPolicy {
        OutboundPolicy::with_allowed(&["ollama", "*.svc.cluster.local"]).unwrap()
    }

    /// With nothing configured, a private destination is refused, and
    /// the refusal names the destination and the setting that allows it.
    #[test]
    fn private_addresses_are_denied_by_default_and_the_refusal_names_the_setting() {
        let p = OutboundPolicy::default();
        for ip in [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.5.5",
            "192.168.0.10",
            "100.64.1.1",
            "fd12::1",
            "::1",
        ] {
            assert!(p.check_ip(ip.parse().unwrap()).is_err(), "{ip}");
        }
        let text = p
            .check_url("http://10.0.0.5:11434/v1")
            .unwrap_err()
            .to_string();
        assert!(
            text.contains("'10.0.0.5'") && text.contains("BRUTOR_OUTBOUND_ALLOW_HOSTS"),
            "{text}"
        );
    }

    #[test]
    fn metadata_and_link_local_are_always_refused() {
        for p in [open(), strict()] {
            assert!(p.check_ip("169.254.169.254".parse().unwrap()).is_err());
            assert!(p.check_ip("fe80::1".parse().unwrap()).is_err());
            assert!(p.check_ip("fd00:ec2::254".parse().unwrap()).is_err());
            assert!(p.check_ip("0.0.0.0".parse().unwrap()).is_err());
            assert!(
                p.check_ip("::ffff:169.254.169.254".parse().unwrap())
                    .is_err()
            );
        }
    }

    #[test]
    fn private_ranges_are_allowed_only_by_an_address_or_range_entry() {
        let ranges =
            OutboundPolicy::with_allowed(&["10.0.0.0/8", "172.16.5.5", "fd00::/8", "[::1]"])
                .unwrap();
        for ip in [
            "10.1.2.3",
            "172.16.5.5",
            "fd12::1",
            "::1",
            "::ffff:10.9.9.9",
        ] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(ranges.check_ip(ip).is_ok(), "{ip} allowed by its entry");
            assert!(strict().check_ip(ip).is_err(), "{ip} denied without one");
        }
        for ip in ["172.16.5.6", "192.168.0.10", "127.0.0.1", "100.64.1.1"] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(ranges.check_ip(ip).is_err(), "{ip} is not listed");
        }
        assert!(strict().check_ip("93.184.216.34".parse().unwrap()).is_ok());
    }

    /// No entry lifts the always-denied ranges: not a CIDR that covers
    /// them, not an allow-listed name that resolves to them.
    #[test]
    fn the_metadata_ranges_cannot_be_allowed() {
        let everything =
            OutboundPolicy::with_allowed(&["0.0.0.0/0", "::/0", "169.254.169.254"]).unwrap();
        for ip in [
            "169.254.169.254",
            "fe80::1",
            "fd00:ec2::254",
            "::ffff:169.254.169.254",
            "64:ff9b::a9fe:a9fe",
            "2002:a9fe:a9fe::1",
            "0.1.2.3",
        ] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(everything.check_ip(ip).is_err(), "{ip}");
            assert!(strict().check_ip_of_allowed_host(ip).is_err(), "{ip}");
        }
        let text = everything
            .check_url("http://169.254.169.254/latest/meta-data/")
            .unwrap_err()
            .to_string();
        assert!(
            text.contains("'169.254.169.254'") && text.contains("never allowed"),
            "{text}"
        );
    }

    /// IPv4 smuggled in IPv6 is judged as the IPv4 address.
    #[test]
    fn ipv4_inside_ipv6_is_judged_as_ipv4() {
        for ip in [
            "::ffff:10.0.0.1",
            "::10.0.0.1",
            "64:ff9b::a00:1",
            "2002:0a00:0001::1",
        ] {
            assert!(strict().check_ip(ip.parse().unwrap()).is_err(), "{ip}");
        }
        assert!(
            strict()
                .check_ip("64:ff9b::5db8:d822".parse().unwrap())
                .is_ok()
        );
    }

    #[test]
    fn an_unreadable_allow_list_entry_is_refused() {
        for bad in [
            "host:8080",
            "http://x",
            "x/y",
            "*",
            "a*.b",
            "10.0.0.0/33",
            "10.0.0.0/",
            "fd00::/129",
            "two words",
        ] {
            assert_eq!(
                OutboundPolicy::with_allowed(&[bad]).map(drop),
                Err(bad.to_string()),
                "{bad}"
            );
        }
        let p = OutboundPolicy::with_allowed(&[" Ollama. ", "", "*.Corp.Example"]).unwrap();
        assert_eq!(p.allow_hosts, ["ollama", "*.corp.example"]);
    }

    #[test]
    fn url_check_covers_scheme_ip_literals_and_allow_list() {
        assert!(
            open()
                .check_url("http://169.254.169.254/latest/meta-data/")
                .is_err()
        );
        assert!(open().check_url("http://[fe80::1]/").is_err());
        assert!(open().check_url("ftp://example.com/").is_err());
        assert!(open().check_url("http://127.0.0.1:11434/v1").is_ok());
        assert!(strict().check_url("http://10.0.0.5:11434/v1").is_err());
        assert!(strict().check_url("http://ollama:11434/v1").is_ok());
        assert!(
            strict()
                .check_url("http://qdrant.svc.cluster.local:6333")
                .is_ok()
        );
        assert!(
            strict().check_url("https://api.openai.com/v1").is_ok(),
            "hostnames are left to DNS"
        );
    }

    #[test]
    fn allow_list_matches_exact_and_suffix() {
        let p = strict();
        assert!(p.host_allowed("ollama"));
        assert!(p.host_allowed("OLLAMA."));
        assert!(p.host_allowed("qdrant.svc.cluster.local"));
        assert!(p.host_allowed("svc.cluster.local"));
        assert!(!p.host_allowed("evil-svc.cluster.local.attacker.com"));
    }

    /// Every error in `err`'s source chain, displayed.
    fn error_chain(err: &dyn std::error::Error) -> String {
        let mut out = err.to_string();
        let mut cur = err.source();
        while let Some(e) = cur {
            out.push_str(" / ");
            out.push_str(&e.to_string());
            cur = e.source();
        }
        out
    }

    /// A mock that answers every request with a redirect to `location`.
    async fn redirecting_to(location: String) -> wiremock::MockServer {
        let server = wiremock::MockServer::start().await;
        wiremock::Mock::given(wiremock::matchers::any())
            .respond_with(
                wiremock::ResponseTemplate::new(302).insert_header("location", location.as_str()),
            )
            .mount(&server)
            .await;
        server
    }

    async fn serving_secret() -> wiremock::MockServer {
        let server = wiremock::MockServer::start().await;
        wiremock::Mock::given(wiremock::matchers::any())
            .respond_with(wiremock::ResponseTemplate::new(200).set_body_string("secret"))
            .mount(&server)
            .await;
        server
    }

    fn denies_loopback_but_allows_localhost() -> OutboundPolicy {
        // The first hop goes to `localhost` (allow-listed); the redirect
        // names the loopback address as an IP literal, which the resolver
        // never sees.
        OutboundPolicy::with_allowed(&["localhost"]).unwrap()
    }

    #[tokio::test]
    async fn a_redirect_to_an_ip_literal_the_policy_denies_is_refused() {
        let secret = serving_secret().await;
        let target = format!("http://127.0.0.1:{}/secret", secret.address().port());
        let first = redirecting_to(target).await;
        let client = client_builder_with(denies_loopback_but_allows_localhost())
            .build()
            .unwrap();
        let err = client
            .get(format!("http://localhost:{}/", first.address().port()))
            .send()
            .await
            .expect_err("the redirect target is denied");
        assert!(err.is_redirect(), "refused as a redirect: {err:?}");
        assert!(
            error_chain(&err).contains("refused by policy"),
            "names the policy: {err:?}"
        );
        assert!(
            secret.received_requests().await.unwrap().is_empty(),
            "the denied destination was never contacted"
        );

        // Control: the same resolver with the redirect policy main.rs used
        // to set (`limited(5)`) follows the hop and reads the secret. This
        // is the behaviour the assertions above rule out.
        let unchecked = reqwest::Client::builder()
            .dns_resolver(Arc::new(PolicyResolver::new(
                denies_loopback_but_allows_localhost(),
            )))
            .redirect(reqwest::redirect::Policy::limited(MAX_REDIRECTS))
            .build()
            .unwrap();
        let leaked = unchecked
            .get(format!("http://localhost:{}/", first.address().port()))
            .send()
            .await
            .unwrap();
        assert_eq!(leaked.text().await.unwrap(), "secret");
    }

    #[tokio::test]
    async fn a_redirect_to_the_metadata_address_is_refused_under_the_default_policy() {
        let first = redirecting_to("http://169.254.169.254/latest/meta-data/".into()).await;
        let client = client_builder_with(open())
            .timeout(std::time::Duration::from_secs(3))
            .build()
            .unwrap();
        let err = client
            .get(first.uri())
            .send()
            .await
            .expect_err("metadata is always denied");
        assert!(err.is_redirect(), "refused before connecting: {err:?}");
        assert!(error_chain(&err).contains("refused by policy"), "{err:?}");
    }

    #[tokio::test]
    async fn a_same_origin_redirect_is_followed() {
        let server = wiremock::MockServer::start().await;
        wiremock::Mock::given(wiremock::matchers::path("/old"))
            .respond_with(
                wiremock::ResponseTemplate::new(302)
                    .insert_header("location", format!("{}/new", server.uri()).as_str()),
            )
            .mount(&server)
            .await;
        wiremock::Mock::given(wiremock::matchers::path("/new"))
            .respond_with(wiremock::ResponseTemplate::new(200).set_body_string("moved"))
            .mount(&server)
            .await;
        let client = client_builder_with(open()).build().unwrap();
        let resp = client
            .get(format!("{}/old", server.uri()))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.text().await.unwrap(), "moved");
    }

    #[tokio::test]
    async fn the_public_fetch_client_follows_an_allowed_cross_origin_redirect() {
        let secret = serving_secret().await;
        let first = redirecting_to(format!("{}/x", secret.uri())).await;
        let client = public_fetch_builder_with(open()).build().unwrap();
        let resp = client.get(first.uri()).send().await.unwrap();
        assert_eq!(resp.status(), 200);
        assert_eq!(resp.text().await.unwrap(), "secret");
    }

    /// A provider key in a custom header never reaches another host.
    #[tokio::test]
    async fn a_credentialed_client_refuses_a_cross_origin_redirect() {
        let other = serving_secret().await;
        let provider = redirecting_to(format!("{}/steal", other.uri())).await;
        let client = client_builder_with(open()).build().unwrap();
        let err = client
            .post(provider.uri())
            .header("x-api-key", "sk-tenant-key")
            .header("api-key", "azure-key")
            .header("x-goog-api-key", "gemini-key")
            .send()
            .await
            .expect_err("cross-origin hop refused");
        assert!(err.is_redirect(), "{err:?}");
        assert!(
            error_chain(&err).contains("cross-origin redirect"),
            "{err:?}"
        );
        assert!(
            other.received_requests().await.unwrap().is_empty(),
            "the other host never saw the request"
        );
        // The policy check still runs first: a denied target names the
        // policy, not the origin rule.
        let meta = redirecting_to("http://169.254.169.254/".into()).await;
        let err = client.get(meta.uri()).send().await.unwrap_err();
        assert!(error_chain(&err).contains("refused by policy"), "{err:?}");

        // Control: reqwest's own policy strips `Authorization` on a host
        // change but forwards these headers. This is the leak ruled out
        // above.
        let unchecked = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::limited(MAX_REDIRECTS))
            .build()
            .unwrap();
        unchecked
            .get(provider.uri())
            .header("x-api-key", "sk-tenant-key")
            .send()
            .await
            .unwrap();
        let got = other.received_requests().await.unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(
            got[0].headers.get("x-api-key").map(|v| v.to_str().unwrap()),
            Some("sk-tenant-key"),
            "without the rule the key follows the redirect"
        );
    }

    #[test]
    fn origins_compare_scheme_host_and_effective_port() {
        let u = |s: &str| reqwest::Url::parse(s).unwrap();
        assert!(same_origin(
            &u("https://API.example.com/a"),
            &u("https://api.example.com:443/b")
        ));
        assert!(!same_origin(
            &u("http://api.example.com/"),
            &u("https://api.example.com/")
        ));
        assert!(!same_origin(
            &u("https://api.example.com/"),
            &u("https://api.example.com:8443/")
        ));
        assert!(!same_origin(
            &u("https://api.example.com/"),
            &u("https://evil.example.com/")
        ));
    }

    #[tokio::test]
    async fn redirects_stop_after_the_limit() {
        // A server that redirects to itself forever.
        let server = wiremock::MockServer::start().await;
        let me = format!("{}/again", server.uri());
        wiremock::Mock::given(wiremock::matchers::any())
            .respond_with(
                wiremock::ResponseTemplate::new(302).insert_header("location", me.as_str()),
            )
            .mount(&server)
            .await;
        let client = client_builder_with(open()).build().unwrap();
        let err = client.get(server.uri()).send().await.expect_err("loops");
        assert!(err.is_redirect(), "{err:?}");
        assert_eq!(
            server.received_requests().await.unwrap().len(),
            MAX_REDIRECTS + 1,
            "the first request plus MAX_REDIRECTS hops"
        );
    }

    #[tokio::test]
    async fn resolver_refuses_a_name_that_only_resolves_to_a_denied_address() {
        // `localhost` resolves to loopback everywhere; unless it is
        // allowed, the resolver must refuse it rather than hand back an
        // address.
        let r = PolicyResolver::new(strict());
        let name: Name = "localhost".parse().unwrap();
        let err = r.resolve(name).await.err().expect("refused");
        assert!(
            err.to_string().contains("BRUTOR_OUTBOUND_ALLOW_HOSTS"),
            "{err}"
        );
        let r = PolicyResolver::new(OutboundPolicy::default());
        let name: Name = "localhost".parse().unwrap();
        assert!(r.resolve(name).await.is_err(), "denied by default");
        let r = PolicyResolver::new(open());
        let name: Name = "localhost".parse().unwrap();
        assert!(r.resolve(name).await.is_ok());
    }
}
