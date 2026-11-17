# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Outbound destination policy: the Control Plane's half of the SSRF guard.

A URL an administrator enters (an MCP server, a model endpoint, an agent, an
IdP, a webhook, a registry, a connector) is fetched with the deployment's own
network position. Left open, it reaches the deployment's database, Redis,
object store and the cloud metadata service. The rules are the Core
Gateway's (`brutor-gateway-core/src/services/outbound_policy.rs`) and KB
Connector Sync's, under the same setting:

* always refused, whatever the allow-list says: link-local (169.254/16,
  fe80::/10), where every cloud metadata service lives (169.254.169.254), the
  IPv6 metadata address ``fd00:ec2::254``, "this network" (0/8), the
  unspecified and broadcast addresses and multicast;
* refused by default: loopback, RFC 1918, carrier-grade NAT (100.64/10),
  IPv6 unique-local (fc00::/7) and site-local (fec0::/10), and a host name
  that resolves only to such addresses;
* ``BRUTOR_OUTBOUND_ALLOW_HOSTS`` lists what is allowed besides: a host name
  (every address it resolves to but the always-refused ones), ``*.domain``
  (the domain and its subdomains), an IP address or a CIDR range. An entry
  it cannot read refuses the start.

IPv4 carried in IPv6 (mapped, compatible, NAT64 ``64:ff9b::/96``, 6to4
``2002::/16``) is judged as the IPv4 address it carries.

When a URL is saved, an IP-literal host is judged here; a host name is
judged where it is fetched, on every address it resolves to
(`app/services/outbound_http.py` here, the resolver in the Core Gateway and
KB Connector Sync), as the Core Gateway's `check_url` does.
"""


import ipaddress
import socket
from typing import Iterable, List, Optional, Union
from urllib.parse import urlsplit

from fastapi import HTTPException
from app.env_vars import BRUTOR_OUTBOUND_ALLOW_HOSTS

IP = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Net = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

#: The setting a refusal names.
ALLOW_SETTING = BRUTOR_OUTBOUND_ALLOW_HOSTS.name

REASON_RESERVED = "reserved address"
REASON_LINK_LOCAL = "link-local address (cloud metadata range)"
REASON_METADATA = "cloud metadata address"
REASON_LOOPBACK = "loopback address"
REASON_RFC1918 = "private (RFC 1918) address"
REASON_CGNAT = "carrier-grade NAT address"
REASON_ULA = "unique-local (private) address"
REASON_SITE_LOCAL = "site-local (private) address"

#: Reasons no allow-list entry lifts.
ALWAYS_DENIED_REASONS = frozenset({REASON_RESERVED, REASON_LINK_LOCAL, REASON_METADATA})

_IMDS_V6 = ipaddress.ip_address("fd00:ec2::254")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ULA = ipaddress.ip_network("fc00::/7")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _canonical(ip: IP) -> IP:
    """The IPv4 address an IPv6 address carries, or the address itself."""
    if isinstance(ip, ipaddress.IPv4Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    packed = ip.packed
    if packed[:12] == bytes(12) and int(ip) > 1:  # IPv4-compatible, not :: or ::1
        return ipaddress.IPv4Address(packed[12:])
    if ip in _NAT64:
        return ipaddress.IPv4Address(packed[12:])
    if ip in _SIX_TO_FOUR:
        return ipaddress.IPv4Address(packed[2:6])
    return ip


def _parse_entry(entry: str) -> Union[str, Net, None]:
    """A network (address or CIDR), a host name or `*.domain` entry (str),
    or None when the entry is neither."""
    raw = entry.strip().rstrip(".").lower()
    try:
        return ipaddress.ip_network(raw.strip("[]"), strict=False)
    except ValueError:
        pass
    bare = raw[2:] if raw.startswith("*.") else raw
    if bare and not bare.startswith(".") and all(c.isalnum() or c in "-_." for c in bare) and bare.isascii():
        return raw
    return None


def check_allow_list(raw: str) -> Optional[str]:
    """The registry's check of BRUTOR_OUTBOUND_ALLOW_HOSTS: what is wrong, or
    None. Never repeats the value."""
    for entry in raw.split(","):
        if entry.strip() and _parse_entry(entry) is None:
            return "an entry is not a host name, *.domain, IP address or CIDR range"
    return None


def _allow_list(entries: Optional[Iterable[str]] = None) -> tuple[List[str], List[Net]]:
    hosts: List[str] = []
    networks: List[Net] = []
    for entry in (entries if entries is not None else BRUTOR_OUTBOUND_ALLOW_HOSTS.items()):
        parsed = _parse_entry(entry) if entry and entry.strip() else None
        if isinstance(parsed, str):
            hosts.append(parsed)
        elif parsed is not None:
            networks.append(parsed)
    return hosts, networks


def host_allowed(host: str, allow_hosts: Optional[Iterable[str]] = None) -> bool:
    """Whether `host` (a host name) is on the allow-list by name."""
    h = host.rstrip(".").lower()
    for entry in _allow_list(allow_hosts)[0]:
        if entry.startswith("*."):
            suffix = entry[2:]
            if h == suffix or h.endswith("." + suffix):
                return True
        elif h == entry:
            return True
    return False


def _always_denied(ip: IP) -> Optional[str]:
    ip = _canonical(ip)
    if isinstance(ip, ipaddress.IPv4Address):
        if ip.packed[0] == 0 or ip == ipaddress.IPv4Address("255.255.255.255") or ip.is_multicast:
            return REASON_RESERVED
        if ip.is_link_local:
            return REASON_LINK_LOCAL
        return None
    if ip.is_unspecified or ip.is_multicast:
        return REASON_RESERVED
    if ip in ipaddress.ip_network("fe80::/10"):
        return REASON_LINK_LOCAL
    if ip == _IMDS_V6:
        return REASON_METADATA
    return None


def _default_denied(ip: IP) -> Optional[str]:
    ip = _canonical(ip)
    if ip.is_loopback:
        return REASON_LOOPBACK
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in n for n in (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                                 ipaddress.ip_network("192.168.0.0/16"))):
            return REASON_RFC1918
        if ip in _CGNAT:
            return REASON_CGNAT
        return None
    if ip in _ULA:
        return REASON_ULA
    if ip in _SITE_LOCAL:
        return REASON_SITE_LOCAL
    return None


def check_ip(ip: IP, allow_hosts: Optional[Iterable[str]] = None) -> Optional[str]:
    """Return a refusal reason, or ``None`` when the address is permitted."""
    reason = _always_denied(ip)
    if reason:
        return reason
    reason = _default_denied(ip)
    if reason and not any(_canonical(ip) in n or ip in n for n in _allow_list(allow_hosts)[1]):
        return reason
    return None


def resolve(host: str) -> List[IP]:
    """The addresses `host` resolves to now; [] when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    out: List[IP] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    return out


def check_url(url: str, *, allow_hosts: Optional[Iterable[str]] = None) -> Optional[str]:
    """Return a refusal reason for ``url``, or ``None`` when it may be used."""
    if not url or not url.strip():
        return "URL is empty"
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return "not a valid URL"
    if parts.scheme not in ("http", "https"):
        return "only http and https destinations are allowed"
    if not host:
        return "URL has no host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return check_ip(ip, allow_hosts)
    # A host name: judged where it is fetched, on every address it resolves to.
    return None


def refusal_message(url: str, what: str, reason: str) -> str:
    """What an administrator reads when a URL is refused: the URL (without
    any credential in it), why, and the setting that decides."""
    try:
        parts = urlsplit(url.strip())
        shown = parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]).geturl()
    except ValueError:
        shown = url
    message = f"{what} '{shown}' is refused by the outbound destination policy: {reason}."
    if any(always in reason for always in ALWAYS_DENIED_REASONS):
        return message + f" This range is never allowed, whatever {ALLOW_SETTING} says."
    if "address" in reason:
        return message + (
            f" Private addresses are refused by default (SSRF). To allow an internal destination, "
            f"an operator adds it to {ALLOW_SETTING} (a host name, *.domain, IP address or CIDR range)."
        )
    return message


def assert_allowed_url(url: str, what: str = "URL") -> None:
    """Raise HTTP 400 when ``url`` is refused by the outbound policy."""
    reason = check_url(url)
    if reason:
        raise HTTPException(status_code=400, detail=refusal_message(url, what, reason))


def assert_allowed_urls_in(config: object, what: str = "connector config") -> None:
    """Walk a JSON-like config and check every http(s) URL string in it."""
    if isinstance(config, dict):
        for v in config.values():
            assert_allowed_urls_in(v, what)
    elif isinstance(config, (list, tuple)):
        for v in config:
            assert_allowed_urls_in(v, what)
    elif isinstance(config, str) and config.strip().lower().startswith(("http://", "https://")):
        assert_allowed_url(config, what)
