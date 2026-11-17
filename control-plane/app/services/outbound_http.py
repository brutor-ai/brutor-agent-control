# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP clients for destinations an administrator entered: the fetch-time half
of the outbound policy (`app/services/outbound_policy.py`).

The check happens where the connection is made: the host of every request,
every redirect hop and every retry is resolved here, its addresses are
judged by the policy, and the connection goes to an address the policy
permits (never re-resolved in between, so rebinding a name to a private
address after the check does not help). TLS still verifies the host name.

A refused destination raises `OutboundRefused`, an `httpx.ConnectError`, so
code that already handles a destination that cannot be reached handles this
one too; its message names the host and `BRUTOR_OUTBOUND_ALLOW_HOSTS`.
Where the request ran inside a task group (the MCP SDK's transport), the
refusal comes out wrapped in an `ExceptionGroup` whose own message says
nothing ("unhandled errors in a TaskGroup"): `error_text` is what an answer
or a log line says of such an error, the refusal where there is one.

First-party clients that only talk to operator-configured services (the
Core Gateway's internal URL, the witness, Qdrant, the MCP Registry, the
licence server, email) do not use these: an operator's own internal
endpoint is not an administrator-entered destination. The Core Gateway
draws the same line.
"""

from __future__ import annotations

import ipaddress
from typing import Any, List, Optional

import anyio
import httpcore
import httpx

from app.services import outbound_policy as policy


class OutboundRefused(httpx.ConnectError):
    """The outbound policy refused the destination."""

    def __init__(self, host: str, reason: str) -> None:
        self.host = host
        self.reason = reason
        message = f"outbound destination '{host}' refused by policy: {reason}."
        if any(always in reason for always in policy.ALWAYS_DENIED_REASONS):
            message += f" This range is never allowed, whatever {policy.ALLOW_SETTING} says."
        else:
            message += (
                f" To allow an internal destination, an operator adds it to {policy.ALLOW_SETTING} "
                "(a host name, *.domain, IP address or CIDR range)."
            )
        super().__init__(message)


def find_refusal(error: Optional[BaseException]) -> Optional[OutboundRefused]:
    """The outbound policy's refusal inside `error`: itself, what caused it,
    or one of an exception group's (the MCP SDK runs its transport in a task
    group), however deeply nested."""
    seen = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OutboundRefused):
            return current
        stack.extend(getattr(current, "exceptions", ()) or ())
        stack.extend((current.__cause__, current.__context__))
    return None


def _leaves(error: BaseException) -> List[BaseException]:
    members = getattr(error, "exceptions", None) if isinstance(error, BaseExceptionGroup) else None
    if not members:
        return [error]
    return [leaf for member in members for leaf in _leaves(member)]


def error_text(error: BaseException, url: Optional[str] = None, what: str = "Destination") -> str:
    """What to say of `error` to an administrator or in a log line: the
    policy's refusal where one is inside it (with `url`, worded as
    `refusal_message` words it), else the messages of an exception group's
    members instead of the group's own, else the error's message."""
    refused = find_refusal(error)
    if refused is not None:
        return policy.refusal_message(url, what, refused.reason) if url else str(refused)
    said: List[str] = []
    for leaf in _leaves(error):
        text = str(leaf) or type(leaf).__name__
        if text not in said:
            said.append(text)
    return "; ".join(said)


def permitted_addresses(host: str) -> List[str]:
    """The addresses `host` may be connected to, in resolution order: those
    the policy permits. Raises `OutboundRefused` when there is none."""
    bare = host.strip("[]")
    try:
        literal = ipaddress.ip_address(bare)
    except ValueError:
        literal = None
    if literal is not None:
        reason = policy.check_ip(literal)
        if reason:
            raise OutboundRefused(host, reason)
        return [bare]
    by_name = policy.host_allowed(host)
    addresses = policy.resolve(host)
    if not addresses:
        raise OutboundRefused(host, "the name does not resolve")
    permitted, first_reason = [], None
    for address in addresses:
        reason = policy._always_denied(address) if by_name else policy.check_ip(address)
        if reason is None:
            if str(address) not in permitted:
                permitted.append(str(address))
        else:
            first_reason = first_reason or reason
    if not permitted:
        raise OutboundRefused(host, f"{host} resolves to a {first_reason}")
    return permitted


def _host(host: Any) -> str:
    return host.decode("ascii") if isinstance(host, bytes) else str(host)


class _AsyncPolicyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = await anyio.to_thread.run_sync(permitted_addresses, _host(host))
        for index, address in enumerate(addresses):
            try:
                return await self._inner.connect_tcp(address, port, timeout, local_address, socket_options)
            except httpcore.ConnectError:
                if index == len(addresses) - 1:
                    raise

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OutboundRefused(str(path), "a Unix socket is not an outbound destination")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _SyncPolicyBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self._inner = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = permitted_addresses(_host(host))
        for index, address in enumerate(addresses):
            try:
                return self._inner.connect_tcp(address, port, timeout, local_address, socket_options)
            except httpcore.ConnectError:
                if index == len(addresses) - 1:
                    raise

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OutboundRefused(str(path), "a Unix socket is not an outbound destination")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


_TRANSPORT_ARGS = ("verify", "cert", "http1", "http2", "limits", "retries", "trust_env")


def _split(kwargs: dict) -> tuple[dict, dict]:
    transport = {k: kwargs.pop(k) for k in _TRANSPORT_ARGS if k in kwargs}
    # The policy decides where a request goes; a proxy from the environment
    # would make that decision instead.
    transport["trust_env"] = False
    kwargs["trust_env"] = False
    return transport, kwargs


class AsyncPolicyTransport(httpx.AsyncHTTPTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        pool = self._pool
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=pool._ssl_context,
            max_connections=pool._max_connections,
            max_keepalive_connections=pool._max_keepalive_connections,
            keepalive_expiry=pool._keepalive_expiry,
            http1=pool._http1,
            http2=pool._http2,
            retries=pool._retries,
            network_backend=_AsyncPolicyBackend(),
        )


class PolicyTransport(httpx.HTTPTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        pool = self._pool
        self._pool = httpcore.ConnectionPool(
            ssl_context=pool._ssl_context,
            max_connections=pool._max_connections,
            max_keepalive_connections=pool._max_keepalive_connections,
            keepalive_expiry=pool._keepalive_expiry,
            http1=pool._http1,
            http2=pool._http2,
            retries=pool._retries,
            network_backend=_SyncPolicyBackend(),
        )


def outbound_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` for administrator-entered destinations. Takes
    what `httpx.AsyncClient` takes but `transport`, `mounts` and `proxy`."""
    transport_args, client_args = _split(kwargs)
    return httpx.AsyncClient(transport=AsyncPolicyTransport(**transport_args), **client_args)


def outbound_client(**kwargs: Any) -> httpx.Client:
    """The synchronous `outbound_async_client`."""
    transport_args, client_args = _split(kwargs)
    return httpx.Client(transport=PolicyTransport(**transport_args), **client_args)


def is_refusal(error: Optional[BaseException]) -> bool:
    """Whether `error` (or what caused it) is the policy's refusal."""
    while error is not None:
        if isinstance(error, OutboundRefused):
            return True
        error = error.__cause__ or error.__context__
    return False
