# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The identity protocol registry.

An identity provider row (`idp_servers`) speaks one protocol. Both editions
have one: OIDC (with plain OAuth 2.0), used for MCP OAuth and agent workload
identity. SAML single sign-on is not in this release: no edition registers
the protocol `saml`, and a request naming it is refused with
`SAML_NOT_IN_THIS_RELEASE`; a stored `saml` row is listed and nothing else.

The registry holds every protocol as an `IdentityProtocolSpec`. The identity
provider request models and the admin router (`/v1/admin/idps`) ask it and
name no protocol but their own. `IDENTITY_PROTOCOLS` holds the one this
package defines, `oidc`; an extension may register more
(`Extension.identity_protocols`). `tests/test_identity_protocols.py`
holds what the registry answers.

The rules the registry exists for:

* **A protocol that is not registered is refused.** `get` returns None for
  it, and every caller treats None as the most restrictive answer: a request
  naming it is invalid, and a stored row of it is shown but cannot be
  changed, deleted, tested, discovered or used. It is never handled as OIDC.
* **A row is an OAuth provider only if its protocol says so** (`oauth`). An
  OAuth client is not attached to, and not registered against, a row of
  another protocol, in any composition.
* **A request carries no field of a protocol that is not registered.** The
  request models declare every column (one schema for both editions); a field that
  is neither common nor owned by a registered protocol must be empty or at
  its default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from app.composition.features import ENTERPRISE_FEATURES

OIDC = "oidc"

# The answer to a request naming the protocol `saml`, in both editions.
SAML_NOT_IN_THIS_RELEASE = (
    "SAML single sign-on is not available in this release; identity providers use the protocol 'oidc'."
)

# What "not set" means for a request field.
EMPTY: Tuple[Any, ...] = (None, "", {}, [])

# The request fields every identity provider has, whatever its protocol.
# (The OAuth capability flags are common: every row has them, and a protocol
# without OAuth sets them off.) A field of the request models that is neither
# here nor in a registered protocol's `fields` belongs to a protocol this
# composition does not have.
COMMON_FIELDS: FrozenSet[str] = frozenset(
    {
        "name",
        "issuer",
        "type",
        "protocol",
        "supports_pkce",
        "supports_refresh_tokens",
        "supports_client_credentials",
        "token_endpoint_auth_methods",
        "scopes_supported",
        "response_types_supported",
        "grant_types_supported",
        "discovery_cache_ttl",
        "enabled",
        "verified",
        "description",
        "logo_url",
    }
)


@dataclass(frozen=True)
class IdentityProtocolSpec:
    """One identity protocol.

    `name` is the value of `idp_servers.protocol`. `label` is how messages
    name it. `feature` is the Enterprise Edition feature it belongs to, or
    None for the Community Edition. `oauth` says that a row of this protocol
    is an OAuth 2.0 / OIDC provider: OAuth clients may be attached to it and
    registered against it.

    `fields` are the request fields that belong to this protocol.
    `exclusive_fields` are those of them that a request of another protocol
    must leave empty; the rest are accepted there (and mean nothing).
    `url_fields` are the fields holding a URL the gateway fetches, for the
    outbound guard.

    `prepare(request, mine, partial)` and `validate(request, mine)` check a
    request model and raise `ValueError` to refuse it; `mine` says whether
    the request names this protocol, `partial` that it is an update (every
    field optional). `prepare` runs first and may normalise the request in
    place; `validate` runs, for a create, after the registry has refused
    another protocol's exclusive fields. Both are called for every registered
    protocol, whichever the request names.

    `create_columns(request)` returns the columns this protocol sets on a new
    row, beyond the ones the router writes itself. Called for every
    registered protocol.

    The message refusing one protocol's exclusive fields on a request of
    another ends with the requesting protocol's `instead_hint` (what to use
    instead) or, when it has none, with the `foreign_hint` of the protocol
    the fields belong to (how to use them).
    """

    name: str
    label: str
    feature: Optional[str] = None
    oauth: bool = False
    fields: FrozenSet[str] = frozenset()
    exclusive_fields: FrozenSet[str] = frozenset()
    url_fields: Tuple[str, ...] = ()
    prepare: Optional[Callable[[Any, bool, bool], None]] = None
    validate: Optional[Callable[[Any, bool], None]] = None
    create_columns: Optional[Callable[[Any], Dict[str, Any]]] = None
    foreign_hint: str = ""
    instead_hint: str = ""


class _RowAfterUpdate:
    """The columns of a row as an update leaves them, read by attribute the
    way a protocol's `validate` reads a request. A column the row does not
    have reads as None."""

    def __init__(self, columns: Mapping[str, Any]) -> None:
        self._columns = dict(columns)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._columns.get(name)


class IdentityProtocolRegistry:
    """The identity protocols that exist, in the order they were registered."""

    def __init__(self, specs: Iterable[IdentityProtocolSpec] = ()) -> None:
        self._specs: Dict[str, IdentityProtocolSpec] = {}
        self.register(specs)

    def register(self, specs: Iterable[IdentityProtocolSpec]) -> None:
        """Add protocols. All of them, or none.

        Refuses a name that is empty, padded, not lower case or already
        registered, a feature that is not an Enterprise Edition feature, a
        field claimed by two protocols or by the common set, and exclusive
        or URL fields that are not among the protocol's fields."""
        merged = dict(self._specs)
        for spec in specs:
            name = spec.name
            if not isinstance(name, str) or not name or name != name.strip() or name != name.lower():
                raise ValueError(f"identity protocols: name is empty, padded or not lower case: {name!r}")
            if name in merged:
                raise ValueError(f"identity protocols: already registered: {name}")
            if spec.feature is not None and spec.feature not in ENTERPRISE_FEATURES:
                raise ValueError(f"identity protocols: {name}: not an Enterprise Edition feature: {spec.feature!r}")
            if not spec.exclusive_fields <= spec.fields or not set(spec.url_fields) <= spec.fields:
                raise ValueError(f"identity protocols: {name}: exclusive and URL fields must be among its fields")
            taken = set(COMMON_FIELDS)
            for other in merged.values():
                taken |= other.fields
            if spec.fields & taken:
                raise ValueError(
                    f"identity protocols: {name}: fields already taken: {sorted(spec.fields & taken)}"
                )
            merged[name] = spec
        self._specs = merged

    def get(self, protocol: Any) -> Optional[IdentityProtocolSpec]:
        """The protocol registered under `protocol`, or None. A value that is
        not a string — a row with no protocol — is never registered."""
        if not isinstance(protocol, str):
            return None
        return self._specs.get(protocol)

    def names(self) -> List[str]:
        return list(self._specs)

    def specs(self) -> Tuple[IdentityProtocolSpec, ...]:
        return tuple(self._specs.values())

    def is_oauth(self, protocol: Any) -> bool:
        """Is `protocol` registered, and are its rows OAuth providers?"""
        spec = self.get(protocol)
        return spec is not None and spec.oauth

    def oauth_names(self) -> List[str]:
        return [spec.name for spec in self._specs.values() if spec.oauth]

    def url_fields(self) -> Tuple[str, ...]:
        """Every field of a registered protocol that holds a fetched URL."""
        return tuple(field for spec in self._specs.values() for field in spec.url_fields)

    def owned_fields(self) -> FrozenSet[str]:
        """The fields a request may carry here: common, or a registered protocol's."""
        owned = set(COMMON_FIELDS)
        for spec in self._specs.values():
            owned |= spec.fields
        return frozenset(owned)

    def check_protocol(self, protocol: Any) -> None:
        """Refuse (`ValueError`) a protocol that is not registered."""
        if self.get(protocol) is None:
            if protocol == "saml":
                raise ValueError(SAML_NOT_IN_THIS_RELEASE)
            raise ValueError(f"protocol must be one of {sorted(self._specs)}; got {protocol!r}")

    def validate_request(self, request: Any, defaults: Dict[str, Any], partial: bool = False) -> None:
        """Check a create or update request model against the registry.

        `request.protocol` is None on an update that does not name one; then
        no protocol counts as the request's own. `defaults` maps a field to
        the value it has when the caller did not set it. `partial` is True
        for an update.

        Raises `ValueError`:
        * for a field no registered protocol owns that is neither empty nor
          at its default;
        * whatever a protocol's `prepare` raises;
        * for a non-empty exclusive field of a protocol the request does not
          name, when the request names one;
        * whatever a protocol's `validate` raises.
        """
        named = getattr(request, "protocol", None)
        declared = set(type(request).model_fields)

        foreign = sorted(
            field
            for field in declared - self.owned_fields()
            if getattr(request, field, None) not in EMPTY and getattr(request, field, None) != defaults.get(field)
        )
        if foreign:
            raise ValueError(
                f"fields of an identity protocol that is not available in this edition: {foreign}"
            )

        for spec in self._specs.values():
            if spec.prepare is not None:
                spec.prepare(request, spec.name == named, partial)

        # An update is checked against the row it changes, which the request
        # model does not have: `validate_update`, called by the router.
        if partial:
            return

        if named is not None:
            own = self._specs.get(named)
            for spec in self._specs.values():
                if spec.name == named:
                    continue
                bad = sorted(
                    field
                    for field in spec.exclusive_fields & declared
                    if getattr(request, field, None) not in EMPTY
                )
                if bad:
                    hint = (own.instead_hint if own is not None else "") or spec.foreign_hint
                    raise ValueError(f"protocol={named} rejects {spec.label}-only fields: {bad}. {hint}".rstrip())

        for spec in self._specs.values():
            if spec.validate is not None:
                spec.validate(request, spec.name == named)

    def _refuse_exclusive_fields_of_others(self, named: str, values: Mapping[str, Any]) -> None:
        own = self._specs[named]
        for spec in self._specs.values():
            if spec.name == named:
                continue
            bad = sorted(field for field in spec.exclusive_fields if values.get(field) not in EMPTY)
            if bad:
                hint = own.instead_hint or spec.foreign_hint
                raise ValueError(f"protocol={named} rejects {spec.label}-only fields: {bad}. {hint}".rstrip())

    def validate_update(self, stored: Mapping[str, Any], sent: Mapping[str, Any]) -> None:
        """Check an update against the row it changes, so that a row of one
        protocol cannot be given another's fields or switched over with its
        old fields still on it.

        `stored` is the row's columns, `sent` what the request set. The
        protocol that counts is the one the request names, otherwise the
        row's. Raises `ValueError`:

        * for a protocol that is not registered;
        * for a non-empty field that only another protocol has, among what
          was sent; and, when the request changes the protocol, among what
          the row would hold afterwards, so the request that switches must
          also clear the old protocol's fields;
        * whatever a protocol's `validate` raises for the row as the update
          leaves it.

        A row that already holds another protocol's fields (written before
        this check existed) can still be edited and cleared: what is stored
        is looked at only for a switch.
        """
        current = stored.get("protocol")
        named = sent.get("protocol")
        if named is None:
            named = current
        self.check_protocol(named)
        after = {**stored, **sent, "protocol": named}
        self._refuse_exclusive_fields_of_others(named, after if named != current else sent)
        row = _RowAfterUpdate(after)
        for spec in self._specs.values():
            if spec.validate is not None:
                spec.validate(row, spec.name == named)

    def create_columns(self, request: Any) -> Dict[str, Any]:
        """The columns the registered protocols set on a new row."""
        columns: Dict[str, Any] = {}
        for spec in self._specs.values():
            if spec.create_columns is not None:
                columns.update(spec.create_columns(request))
        return columns

    def __len__(self) -> int:
        return len(self._specs)


# ---------------------------------------------------------------------------
# OIDC (with plain OAuth 2.0): the Community Edition's protocol. Its columns
# are written by the router itself.
# ---------------------------------------------------------------------------

OIDC_URL_FIELDS: Tuple[str, ...] = (
    "well_known_url",
    "authorization_endpoint",
    "token_endpoint",
    "revocation_endpoint",
    "introspection_endpoint",
    "jwks_uri",
    "userinfo_endpoint",
)

OIDC_PROTOCOL = IdentityProtocolSpec(
    name=OIDC,
    label="OIDC",
    oauth=True,
    fields=frozenset(OIDC_URL_FIELDS) | {"registration_endpoint", "workload_audiences"},
    # The audiences of workload tokens were never refused on another
    # protocol's request (they are stored, and mean nothing there).
    exclusive_fields=frozenset(OIDC_URL_FIELDS) | {"registration_endpoint"},
    url_fields=OIDC_URL_FIELDS,
    foreign_hint="Set protocol='oidc' to use these.",
)


BUILTIN_IDENTITY_PROTOCOLS: Tuple[IdentityProtocolSpec, ...] = (OIDC_PROTOCOL,)

# The registry the application reads. Extensions add to it before the
# application serves.
IDENTITY_PROTOCOLS = IdentityProtocolRegistry(BUILTIN_IDENTITY_PROTOCOLS)
