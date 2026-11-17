# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Whose forwarded headers the Control Plane believes (BRUTOR_TRUSTED_PROXIES)

A request's client IP is the TCP peer's address unless the peer is in
`BRUTOR_TRUSTED_PROXIES`; then it is the right-most `X-Forwarded-For`
address that is not itself a trusted proxy. `X-Forwarded-Proto` and
`X-Forwarded-Host` are used only when the peer is a trusted proxy. Unset or
empty: no forwarded header is believed. The Core Gateway reads the same
variable by the same rules.

The details:

- every `X-Forwarded-For` header, in order, joined; entries trimmed, empty
  ones dropped, a port (`1.2.3.4:80`, `[2001:db8::1]:80`) removed;
- walked from the right: a trusted proxy is skipped, the first address that
  is not one is the client; an entry that is not an address stops the walk
  and the client is the peer; when every entry is a trusted proxy, the
  left-most one is the client;
- `X-Forwarded-Proto`, `X-Forwarded-Host`: the right-most value of the last
  header, used only when it is `http`/`https`, or a plausible `host[:port]`;
- an IPv4-mapped IPv6 address is the IPv4 address. `Forwarded`, `X-Real-IP`
  and the like are never read.

One implementation: `TrustedProxyMiddleware` (outermost, `app/app.py`)
rewrites the ASGI scope (`client`, `scheme`, `Host`), and `client_ip` is the
one helper every audit row, DCR and session reads the client's address
through. Uvicorn's and Gunicorn's own proxy-header handling is switched off
(`app/launcher.py`, `gunicorn.conf.py`).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from app import env_vars

_Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
Headers = Iterable[Tuple[bytes, bytes]]

# Set in the scope once the middleware has resolved it, so `client_ip` does
# not resolve a second time.
_RESOLVED = "brutor.trusted_proxies.resolved"

ACCEPTED = "comma-separated IP addresses and CIDR ranges (no host names, no *)"


def _network(entry: str) -> Optional[_Network]:
    try:
        network = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None
    if isinstance(network, ipaddress.IPv6Network) and network.prefixlen >= 96:
        mapped = network.network_address.ipv4_mapped
        if mapped is not None:
            return ipaddress.ip_network(f"{mapped}/{network.prefixlen - 96}")
    return network


def check(raw: str) -> Optional[str]:
    """What the variable must be, when `raw` is not that (the env registry's
    check: refused at start, the value is never printed)."""
    for entry in raw.split(","):
        entry = entry.strip()
        if entry and _network(entry) is None:
            return ACCEPTED
    return None


@lru_cache(maxsize=8)
def parse_trusted(raw: str) -> Tuple[_Network, ...]:
    """The trusted networks of a value of the variable (invalid entries are
    refused by `check` before anything reads them; here they are skipped)."""
    return tuple(n for n in (_network(e.strip()) for e in raw.split(",") if e.strip()) if n is not None)


def _configured() -> Tuple[_Network, ...]:
    return parse_trusted(",".join(env_vars.BRUTOR_TRUSTED_PROXIES.items()))


def _address(text: str) -> Optional[_Address]:
    """An address, without a port; IPv4-mapped IPv6 as IPv4."""
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        text = text[1:end]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(address: Optional[_Address], networks: Sequence[_Network]) -> bool:
    return address is not None and any(address in network for network in networks)


def _values(headers: Headers, name: bytes) -> List[str]:
    return [value.decode("latin-1") for key, value in headers if key.lower() == name]


def _last_value(headers: Headers, name: bytes) -> Optional[str]:
    values = _values(headers, name)
    if not values:
        return None
    parts = [part.strip() for part in values[-1].split(",")]
    return parts[-1] if parts else None


def _plausible_host(value: str) -> bool:
    if not value or not value.isascii() or any(c.isspace() or c in "/\\@?#" for c in value):
        return False
    host, port = value, None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return False
        host, rest = value[: end + 1], value[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return False
            port = rest[1:]
    elif ":" in value:
        host, _, port = value.rpartition(":")
    if not host or (port is not None and not (port.isdigit() and 1 <= int(port) <= 65535)):
        return False
    return True


@dataclass(frozen=True)
class Resolved:
    client: Optional[str]
    # None: keep what the connection says.
    scheme: Optional[str] = None
    host: Optional[str] = None


def resolve(peer: Optional[str], headers: Headers, trusted: Sequence[_Network]) -> Resolved:
    """The client, scheme and host of a request from `peer` with `headers`."""
    headers = list(headers)
    peer_address = _address(peer) if peer else None
    shown_peer = str(peer_address) if peer_address is not None else peer
    if not _trusted(peer_address, trusted):
        return Resolved(shown_peer)

    entries = [e.strip() for v in _values(headers, b"x-forwarded-for") for e in v.split(",") if e.strip()]
    client = shown_peer
    if entries:
        walked: Optional[_Address] = None
        for entry in reversed(entries):
            address = _address(entry)
            if address is None:
                walked = None
                break
            walked = address
            if not _trusted(address, trusted):
                break
        if walked is not None:
            client = str(walked)

    scheme = (_last_value(headers, b"x-forwarded-proto") or "").lower()
    host = _last_value(headers, b"x-forwarded-host")
    return Resolved(
        client,
        scheme if scheme in ("http", "https") else None,
        host if host is not None and _plausible_host(host) else None,
    )


def _peer(scope) -> Optional[str]:
    client = scope.get("client")
    return client[0] if client else None


def client_ip(request) -> Optional[str]:
    """The client's IP address of a request (None without a request or a
    peer). The one place any code asks for it."""
    if request is None:
        return None
    scope = request.scope
    if scope.get(_RESOLVED):
        return _peer(scope)
    if not scope.get("client"):
        return None
    return resolve(_peer(scope), scope.get("headers") or [], _configured()).client


class TrustedProxyMiddleware:
    """Applies the rules to the ASGI scope: `client`, `scheme`, `Host`."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not scope.get(_RESOLVED):
            # In place, as Uvicorn's own proxy-header middleware does: the
            # middleware outside shares this scope (its `state`).
            client = scope.get("client")
            if client:
                headers = list(scope.get("headers") or [])
                resolved = resolve(client[0], headers, _configured())
                port = client[1] if resolved.client == client[0] else 0
                scope["client"] = (resolved.client, port)
                if resolved.scheme:
                    scope["scheme"] = (
                        resolved.scheme.replace("http", "ws") if scope["type"] == "websocket" else resolved.scheme
                    )
                if resolved.host:
                    scope["headers"] = [(k, v) for k, v in headers if k.lower() != b"host"] + [
                        (b"host", resolved.host.encode("latin-1"))
                    ]
            scope[_RESOLVED] = True
        await self.app(scope, receive, send)
