// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! URL analyzer with SSRF classification.
//!
//! `url_ip_class` is the most load-bearing fact — operators key
//! the `host_in_private_range` predicate off it. We classify per
//! RFC 1918 (private), 169.254/16 (link-local, includes AWS / GCP
//! metadata IPs), 127/8 + ::1 (loopback), 100.64/10 (CG-NAT),
//! 224/4 + ff00::/8 (multicast), 0.0.0.0/8 + :: (unspecified).
//! DNS-resolved names with well-known metadata hostnames
//! (`metadata.google.internal`, etc.) are also flagged.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use super::{ArgFacts, ArgKindTag};

/// Well-known cloud metadata hostnames that should never be
/// reached via tool calls. Resolved lazily — we don't do live
/// DNS, we just match the literal string.
const METADATA_HOSTS: &[&str] = &["metadata.google.internal", "metadata", "metadata.azure.com"];

pub fn analyze(input: &str) -> ArgFacts {
    let mut facts = ArgFacts {
        kind: ArgKindTag::Url,
        raw_excerpt: super::truncate_excerpt(input),
        ..Default::default()
    };

    let trimmed = input.trim();
    let parsed = match url::Url::parse(trimmed) {
        Ok(u) => u,
        Err(_) => {
            // Not an absolute URL — e.g. a bare `evil.com/x` a fetch tool may
            // still resolve. Every URL predicate holds for it, so a
            // host allow-list cannot be passed by dropping the scheme.
            facts.url_ip_class = Some("unknown".into());
            facts.url_parse_error = true;
            return facts;
        }
    };

    facts.url_scheme = Some(parsed.scheme().to_ascii_lowercase());

    let host_str = parsed.host_str().map(|s| s.to_ascii_lowercase());
    facts.url_host = host_str.clone();

    facts.url_ip_class = Some(classify_host(host_str.as_deref()));

    facts
}

pub(crate) fn classify_host(host: Option<&str>) -> String {
    let h = match host {
        Some(h) => h,
        None => return "unknown".into(),
    };

    if METADATA_HOSTS.contains(&h) {
        return "metadata".into();
    }

    // Try IP literal first
    if let Ok(ip) = h.parse::<IpAddr>() {
        return classify_ip(ip).into();
    }
    // Strip surrounding brackets (IPv6 literal in URL)
    if h.starts_with('[') && h.ends_with(']') {
        if let Ok(ip) = h[1..h.len() - 1].parse::<IpAddr>() {
            return classify_ip(ip).into();
        }
    }

    "public".into()
}

fn classify_ip(ip: IpAddr) -> &'static str {
    match ip {
        IpAddr::V4(v4) => classify_ipv4(v4),
        IpAddr::V6(v6) => classify_ipv6(v6),
    }
}

fn classify_ipv4(ip: Ipv4Addr) -> &'static str {
    let oct = ip.octets();
    // Cloud metadata IPs (covered by link_local, but classified
    // specifically so audit logs make the SSRF intent obvious).
    if ip == Ipv4Addr::new(169, 254, 169, 254) {
        return "metadata";
    }
    if oct[0] == 127 {
        "loopback"
    // The three RFC1918 ranges, in one arm. They were separate `else if`s
    // returning the same label, which reads fine but is three identical
    // blocks as far as clippy is concerned.
    } else if oct[0] == 10
        || (oct[0] == 172 && (16..=31).contains(&oct[1]))
        || (oct[0] == 192 && oct[1] == 168)
    {
        "private"
    } else if oct[0] == 169 && oct[1] == 254 {
        "link_local"
    } else if oct[0] == 100 && (64..=127).contains(&oct[1]) {
        "cgnat"
    } else if oct[0] == 0 {
        "unspecified"
    } else if oct[0] >= 224 && oct[0] <= 239 {
        "multicast"
    } else if oct[0] == 255 {
        "broadcast"
    } else {
        "public"
    }
}

fn classify_ipv6(ip: Ipv6Addr) -> &'static str {
    if ip == Ipv6Addr::LOCALHOST {
        return "loopback";
    }
    if ip == Ipv6Addr::UNSPECIFIED {
        return "unspecified";
    }
    let seg = ip.segments();
    let first = seg[0];
    if (first & 0xfe00) == 0xfc00 {
        "private" // unique local fc00::/7
    } else if (first & 0xffc0) == 0xfe80 {
        "link_local" // fe80::/10
    } else if (first & 0xff00) == 0xff00 {
        "multicast"
    } else {
        "public"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_https_url_classified_as_public() {
        let f = analyze("https://example.com/path?q=1");
        assert_eq!(f.url_scheme.as_deref(), Some("https"));
        assert_eq!(f.url_host.as_deref(), Some("example.com"));
        assert_eq!(f.url_ip_class.as_deref(), Some("public"));
    }

    #[test]
    fn aws_metadata_ip_classified_as_metadata() {
        let f = analyze("http://169.254.169.254/latest/meta-data/");
        assert_eq!(f.url_ip_class.as_deref(), Some("metadata"));
    }

    #[test]
    fn gcp_metadata_hostname_classified_as_metadata() {
        let f = analyze("http://metadata.google.internal/v1/");
        assert_eq!(f.url_ip_class.as_deref(), Some("metadata"));
    }

    #[test]
    fn rfc1918_10_classified_as_private() {
        assert_eq!(
            analyze("http://10.0.0.1/").url_ip_class.as_deref(),
            Some("private")
        );
    }

    #[test]
    fn rfc1918_172_16_to_31_classified_as_private() {
        for octet in [16, 24, 31] {
            let url = format!("http://172.{octet}.0.1/");
            assert_eq!(
                analyze(&url).url_ip_class.as_deref(),
                Some("private"),
                "172.{octet}.0.1 should be private",
            );
        }
    }

    #[test]
    fn rfc1918_172_15_and_32_are_public() {
        assert_eq!(
            analyze("http://172.15.0.1/").url_ip_class.as_deref(),
            Some("public")
        );
        assert_eq!(
            analyze("http://172.32.0.1/").url_ip_class.as_deref(),
            Some("public")
        );
    }

    #[test]
    fn rfc1918_192_168_classified_as_private() {
        assert_eq!(
            analyze("http://192.168.1.1/").url_ip_class.as_deref(),
            Some("private")
        );
    }

    #[test]
    fn loopback_127_classified_as_loopback() {
        assert_eq!(
            analyze("http://127.0.0.1/").url_ip_class.as_deref(),
            Some("loopback")
        );
        assert_eq!(
            analyze("http://127.255.255.255/").url_ip_class.as_deref(),
            Some("loopback")
        );
    }

    #[test]
    fn ipv6_loopback_classified_as_loopback() {
        assert_eq!(
            analyze("http://[::1]/").url_ip_class.as_deref(),
            Some("loopback")
        );
    }

    #[test]
    fn ipv6_unique_local_classified_as_private() {
        assert_eq!(
            analyze("http://[fc00::1]/").url_ip_class.as_deref(),
            Some("private")
        );
    }

    #[test]
    fn ipv6_link_local_classified_as_link_local() {
        assert_eq!(
            analyze("http://[fe80::1]/").url_ip_class.as_deref(),
            Some("link_local")
        );
    }

    #[test]
    fn cgnat_100_64_classified_as_cgnat() {
        assert_eq!(
            analyze("http://100.64.0.1/").url_ip_class.as_deref(),
            Some("cgnat")
        );
    }

    #[test]
    fn unparseable_url_classified_as_unknown() {
        assert_eq!(
            analyze("not a url").url_ip_class.as_deref(),
            Some("unknown")
        );
    }

    #[test]
    fn scheme_is_lowercased() {
        assert_eq!(
            analyze("HTTPS://example.com").url_scheme.as_deref(),
            Some("https")
        );
    }
}
