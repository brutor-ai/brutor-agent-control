// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Whose forwarded headers the gateway believes (`BRUTOR_TRUSTED_PROXIES`).
//!
//! The same variable and the same rules as the Control Plane
//! (`app/middleware/trusted_proxies.py`):
//!
//! * the value is comma-separated IP addresses and CIDR ranges; whitespace
//!   and empty entries are ignored; anything else refuses the start (the
//!   name is reported, never the value); a range with host bits set is its
//!   network. Unset or empty: no forwarded header is believed;
//! * a request's client IP is the TCP peer's address unless the peer is a
//!   trusted proxy; then every `X-Forwarded-For` header, in order, is
//!   joined, split on `,`, trimmed, empty entries dropped, a port
//!   (`1.2.3.4:80`, `[2001:db8::1]:80`) removed, and the entries are walked
//!   from the right: a trusted proxy is skipped, the first address that is
//!   not one is the client, an entry that is not an address stops the walk
//!   (the client is the peer), and when every entry is a trusted proxy the
//!   left-most is the client;
//! * an IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) is the IPv4 address,
//!   both in the list and in the request; the client IP is written in
//!   canonical form;
//! * `Forwarded` (RFC 7239), `X-Real-IP`, `True-Client-IP` and
//!   `CF-Connecting-IP` are never read.
//!
//! The Control Plane also takes `X-Forwarded-Proto` / `X-Forwarded-Host`
//! from a trusted proxy to build absolute URLs. The gateway builds none from
//! a request: every URL it advertises comes from `BRUTOR_GATEWAY_PUBLIC_URL`,
//! so it reads neither header.
//!
//! This is the only module that names a forwarded header, and
//! `routes::request::ClientIp` the only reader of the peer address
//! (`tests/trusted_proxies_guard.rs`).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use axum::http::HeaderMap;

/// What the variable accepts (its registry entry says the same).
pub const ACCEPTED: &str = "comma-separated IP addresses and CIDR ranges (no host names, no *)";

use std::sync::atomic::{AtomicBool, Ordering};

const X_FORWARDED_FOR: &str = "x-forwarded-for";

/// One trusted network: an address and a prefix length.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Network {
    addr: IpAddr,
    prefix: u8,
}

impl Network {
    /// `10.0.0.0/8`, `fd00::/8`, `10.0.0.5`, `::ffff:10.0.0.0/104`.
    fn parse(entry: &str) -> Option<Self> {
        let (addr, prefix) = match entry.split_once('/') {
            Some((addr, prefix)) => (addr, Some(prefix)),
            None => (entry, None),
        };
        let addr: IpAddr = addr.parse().ok()?;
        let max = if addr.is_ipv4() { 32 } else { 128 };
        let prefix = match prefix {
            None => max,
            Some(p) if !p.is_empty() && p.len() <= 3 && p.bytes().all(|b| b.is_ascii_digit()) => {
                p.parse().ok()?
            }
            Some(_) => return None,
        };
        if prefix > max {
            return None;
        }
        // An IPv4-mapped range is the IPv4 range it maps.
        if let IpAddr::V6(v6) = addr
            && prefix >= 96
            && let Some(v4) = v6.to_ipv4_mapped()
        {
            return Some(Self::masked(IpAddr::V4(v4), prefix - 96));
        }
        Some(Self::masked(addr, prefix))
    }

    /// The network of `addr/prefix` (host bits cleared).
    fn masked(addr: IpAddr, prefix: u8) -> Self {
        let addr = match addr {
            IpAddr::V4(a) => {
                let mask = u32::MAX.checked_shl(32 - u32::from(prefix)).unwrap_or(0);
                IpAddr::V4(Ipv4Addr::from(u32::from(a) & mask))
            }
            IpAddr::V6(a) => {
                let mask = u128::MAX.checked_shl(128 - u32::from(prefix)).unwrap_or(0);
                IpAddr::V6(Ipv6Addr::from(u128::from(a) & mask))
            }
        };
        Self { addr, prefix }
    }

    /// Whether `ip` (already canonical) is in this network.
    fn contains(&self, ip: IpAddr) -> bool {
        self.addr == Self::masked(ip, self.prefix).addr && self.addr.is_ipv4() == ip.is_ipv4()
    }
}

/// An IPv4-mapped IPv6 address as the IPv4 address; any other as itself.
fn canonical(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => v6.to_ipv4_mapped().map_or(ip, IpAddr::V4),
        v4 => v4,
    }
}

/// An `X-Forwarded-For` entry's address, without a port: `1.2.3.4`,
/// `1.2.3.4:80`, `2001:db8::1`, `[2001:db8::1]`, `[2001:db8::1]:80`.
fn entry_address(entry: &str) -> Option<IpAddr> {
    let entry = entry.trim();
    let text = if let Some(rest) = entry.strip_prefix('[') {
        &rest[..rest.find(']')?]
    } else if entry.matches(':').count() == 1 {
        entry.split(':').next().unwrap_or(entry)
    } else {
        entry
    };
    text.parse().ok().map(canonical)
}

/// A value of `BRUTOR_TRUSTED_PROXIES` with an entry that is neither an
/// address nor a CIDR range. Holds nothing of the value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidTrustedProxies;

impl std::fmt::Display for InvalidTrustedProxies {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "BRUTOR_TRUSTED_PROXIES is not valid: it must be {ACCEPTED}"
        )
    }
}

impl std::error::Error for InvalidTrustedProxies {}

/// The proxies whose `X-Forwarded-For` is believed. [`Default`] is the empty
/// list: no forwarded header is believed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TrustedProxies {
    networks: Vec<Network>,
}

impl TrustedProxies {
    /// The list a value of `BRUTOR_TRUSTED_PROXIES` names, or `Err` when an
    /// entry is not an address or a CIDR range (the error holds no value).
    pub fn parse(raw: &str) -> Result<Self, InvalidTrustedProxies> {
        let mut networks = Vec::new();
        for entry in raw.split(',').map(str::trim).filter(|e| !e.is_empty()) {
            networks.push(Network::parse(entry).ok_or(InvalidTrustedProxies)?);
        }
        Ok(Self { networks })
    }

    /// Whether no proxy is trusted.
    pub fn is_empty(&self) -> bool {
        self.networks.is_empty()
    }

    fn trusts(&self, ip: IpAddr) -> bool {
        let ip = canonical(ip);
        self.networks.iter().any(|n| n.contains(ip))
    }

    /// Log, at WARN and once per `reported` flag, that a request carried
    /// `X-Forwarded-For` while no proxy is trusted: the header is ignored, so
    /// behind a proxy every caller has the proxy's address (and network
    /// facts about the caller are the proxy's). `true` when this call wrote
    /// the warning.
    fn report_ignored_forwarded(&self, headers: &HeaderMap, reported: &AtomicBool) -> bool {
        if !self.is_empty() || !headers.contains_key(X_FORWARDED_FOR) {
            return false;
        }
        if reported.swap(true, Ordering::Relaxed) {
            return false;
        }
        tracing::warn!(
            "a request carried X-Forwarded-For but BRUTOR_TRUSTED_PROXIES is empty: the header \
             is ignored and each caller's address is its peer's (behind a load balancer, the \
             load balancer's). \
             List the proxies in BRUTOR_TRUSTED_PROXIES. Reported once per process."
        );
        true
    }

    /// The client IP of a request from `peer` with `headers`, in canonical
    /// form. The rules are the module's.
    pub fn client_ip(&self, headers: &HeaderMap, peer: IpAddr) -> IpAddr {
        static REPORTED: AtomicBool = AtomicBool::new(false);
        self.report_ignored_forwarded(headers, &REPORTED);
        let peer = canonical(peer);
        if !self.trusts(peer) {
            return peer;
        }
        let entries: Vec<&str> = headers
            .get_all(X_FORWARDED_FOR)
            .iter()
            .filter_map(|v| v.to_str().ok())
            .flat_map(|v| v.split(','))
            .map(str::trim)
            .filter(|e| !e.is_empty())
            .collect();
        let mut walked = None;
        for entry in entries.iter().rev() {
            let Some(address) = entry_address(entry) else {
                // Not an address: nothing left of it is believed.
                return peer;
            };
            walked = Some(address);
            if !self.trusts(address) {
                break;
            }
        }
        walked.unwrap_or(peer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    /// With no trusted proxy, a request that carries `X-Forwarded-For` is
    /// told about once per process (the header is ignored, so every caller
    /// behind the operator's proxy has the proxy's address); never with a
    /// trusted list, never without the header.
    #[test]
    fn an_ignored_forwarded_header_is_reported_once() {
        let flag = std::sync::atomic::AtomicBool::new(false);
        let none = TrustedProxies::default();
        assert!(!none.report_ignored_forwarded(&HeaderMap::new(), &flag));
        assert!(none.report_ignored_forwarded(&xff(&["203.0.113.7"]), &flag));
        assert!(
            !none.report_ignored_forwarded(&xff(&["203.0.113.8"]), &flag),
            "once"
        );
        let fresh = std::sync::atomic::AtomicBool::new(false);
        assert!(!trusted("10.0.0.0/8").report_ignored_forwarded(&xff(&["203.0.113.7"]), &fresh));
    }

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    fn xff(values: &[&str]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for v in values {
            h.append(X_FORWARDED_FOR, HeaderValue::from_str(v).unwrap());
        }
        h
    }

    fn trusted(raw: &str) -> TrustedProxies {
        TrustedProxies::parse(raw).unwrap()
    }

    fn client(list: &str, peer: &str, values: &[&str]) -> String {
        trusted(list).client_ip(&xff(values), ip(peer)).to_string()
    }

    #[test]
    fn the_value_is_addresses_and_ranges_and_nothing_else() {
        assert!(trusted("").is_empty());
        assert!(trusted(" , ,").is_empty());
        let t = trusted(" 10.0.0.0/8 , 192.168.1.10,fd00::/8,, 2001:db8::1 ");
        assert_eq!(t.networks.len(), 4);
        for bad in [
            "*",
            "proxy.internal",
            "10.0.0.0/33",
            "fd00::/129",
            "10.0.0.0/",
            "10.0.0.0/x",
            "10.0.0.0/-1",
            "[fd00::1]",
            "10.0.0.1:80",
            "10.0.0.0/8,localhost",
            "0.0.0.0/8 junk",
        ] {
            assert_eq!(
                TrustedProxies::parse(bad),
                Err(InvalidTrustedProxies),
                "{bad}"
            );
        }
        // host bits set: accepted as its network
        assert_eq!(trusted("10.0.0.1/8"), trusted("10.0.0.0/8"));
    }

    #[test]
    fn an_untrusted_peer_is_the_client_whatever_the_headers_say() {
        assert_eq!(client("", "203.0.113.7", &["10.9.9.9"]), "203.0.113.7");
        assert_eq!(
            client("10.0.0.0/8", "203.0.113.7", &["10.9.9.9"]),
            "203.0.113.7"
        );
        // X-Real-IP and Forwarded are never read, even from a trusted proxy
        let mut h = HeaderMap::new();
        h.insert("x-real-ip", HeaderValue::from_static("1.1.1.1"));
        h.insert("forwarded", HeaderValue::from_static("for=1.1.1.1"));
        assert_eq!(
            trusted("10.0.0.0/8").client_ip(&h, ip("10.0.0.2")),
            ip("10.0.0.2")
        );
    }

    #[test]
    fn behind_trusted_proxies_the_client_is_the_right_most_untrusted_entry() {
        let list = "10.0.0.0/8";
        // a forged left-most entry is not believed
        assert_eq!(
            client(list, "10.0.0.2", &["6.6.6.6, 198.51.100.4, 10.0.0.3"]),
            "198.51.100.4"
        );
        // headers joined in order
        assert_eq!(
            client(
                list,
                "10.0.0.2",
                &["6.6.6.6", "198.51.100.4,10.0.0.3", " , "]
            ),
            "198.51.100.4"
        );
        // no header, or only empty entries: the peer
        assert_eq!(client(list, "10.0.0.2", &[]), "10.0.0.2");
        assert_eq!(client(list, "10.0.0.2", &[" , ,"]), "10.0.0.2");
        // every entry trusted: the left-most
        assert_eq!(
            client(list, "10.0.0.2", &["10.1.1.1, 10.2.2.2"]),
            "10.1.1.1"
        );
        // a non-address stops the walk: the peer
        assert_eq!(
            client(list, "10.0.0.2", &["198.51.100.4, unknown, 10.0.0.3"]),
            "10.0.0.2"
        );
        assert_eq!(client(list, "10.0.0.2", &["proxy.example"]), "10.0.0.2");
        // ... but entries right of it are walked first
        assert_eq!(
            client(list, "10.0.0.2", &["unknown, 198.51.100.4"]),
            "198.51.100.4"
        );
    }

    #[test]
    fn ports_are_removed_and_addresses_are_canonical() {
        let list = "10.0.0.0/8,fd00::/8";
        assert_eq!(
            client(list, "10.0.0.2", &["198.51.100.4:5678"]),
            "198.51.100.4"
        );
        assert_eq!(
            client(list, "10.0.0.2", &["[2001:db8::1]:5678"]),
            "2001:db8::1"
        );
        assert_eq!(client(list, "10.0.0.2", &["[2001:db8::1]"]), "2001:db8::1");
        assert_eq!(client(list, "10.0.0.2", &["2001:DB8::1"]), "2001:db8::1");
        assert_eq!(client(list, "fd00::5", &["2001:db8::1"]), "2001:db8::1");
        // a trusted entry with a port is still trusted
        assert_eq!(
            client(list, "10.0.0.2", &["198.51.100.4, 10.0.0.3:443"]),
            "198.51.100.4"
        );
        // malformed brackets: not an address
        assert_eq!(client(list, "10.0.0.2", &["[2001:db8::1"]), "10.0.0.2");
    }

    #[test]
    fn ipv4_mapped_addresses_are_compared_and_written_as_ipv4() {
        // a mapped peer against an IPv4 list
        assert_eq!(
            client("10.0.0.0/8", "::ffff:10.0.0.2", &["198.51.100.4"]),
            "198.51.100.4"
        );
        // an untrusted mapped peer is written as IPv4
        assert_eq!(client("", "::ffff:203.0.113.7", &[]), "203.0.113.7");
        // a mapped entry, trusted and not
        assert_eq!(
            client(
                "10.0.0.0/8",
                "10.0.0.2",
                &["::ffff:198.51.100.4, ::ffff:10.0.0.3"]
            ),
            "198.51.100.4"
        );
        // a mapped range in the list is the IPv4 range
        assert_eq!(
            client("::ffff:10.0.0.0/104", "10.0.0.2", &["198.51.100.4"]),
            "198.51.100.4"
        );
        // an IPv4 range never contains a non-mapped IPv6 address
        assert_eq!(
            client("0.0.0.0/0", "2001:db8::9", &["198.51.100.4"]),
            "2001:db8::9"
        );
    }
}
