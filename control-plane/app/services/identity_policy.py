# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The identity policy: what credentials a group's traffic must present

A call is judged by the **effective** identity policy of the group it acts
in. That policy is resolved from contributions, field by field, the
stricter value winning:

1. the development default (audit, any key);
2. the tenant's identity default, when one is set;
3. for every group of the acting group's ancestor chain, root first:
   its explicit policy; the production floor when it is an AI System in a
   production stage (enforce, workload identity, no personal keys); the
   high-risk floor when its risk tier is high (enforce, no personal keys);
4. workload identity with proof of possession when the chain holds a
   high-risk group and an AI System in production.

So the production floor binds the system itself as well as everything
below it: no explicit or tenant policy relaxes it. Only an exception to the
workload-identity gate requirement lowers its level, to `bound_key`, and
never for a high-risk chain.

The core runs the same algorithm on every call (its resolver and this one
share the cases the core keeps, `tests/test_identity_policy_resolver.py`).
This plane resolves to show an administrator where each value comes from,
to refuse a write that would relax what a group inherits, to refuse keys
the policy would refuse at use, and for the lifecycle gate.

Policies are plain dicts in their stored JSON shape. A stored value that is
not a valid policy is read as the strictest policy (a writer bug must not
open the floor), and the error is logged.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.constants.identity import (
    CREDENTIAL_LEVELS,
    HIGH_RISK_TIERS,
    LADDER_KINDS,
    POLICY_FIELDS,
    POLICY_MODES,
    PRODUCTION_STAGES,
)

logger = logging.getLogger(__name__)

TOKEN_LIFETIME_CEILING_S = 3600
DEFAULT_MAX_USER_KEY_TTL_DAYS = 90
MAX_ALLOWED_ISSUERS = 50
TTL_DAYS_RANGE = (1, 3650)
LIFETIME_RANGE = (60, TOKEN_LIFETIME_CEILING_S)

DEVELOPMENT_DEFAULT: Dict[str, Any] = {
    "mode": "audit",
    "min_assurance": "key",
    "allow_user_keys": True,
    "allowed_issuers": None,
    "max_token_lifetime_s": TOKEN_LIFETIME_CEILING_S,
    "max_key_ttl_days": None,
}

#: What an invalid stored policy is read as.
STRICTEST: Dict[str, Any] = {
    "mode": "enforce",
    "min_assurance": "workload_pop",
    "allow_user_keys": False,
    "allowed_issuers": [],
    "max_token_lifetime_s": 60,
    "max_key_ttl_days": 1,
}

PRODUCTION_FLOOR: Dict[str, Any] = {"mode": "enforce", "min_assurance": "workload", "allow_user_keys": False}
HIGH_RISK_FLOOR: Dict[str, Any] = {"mode": "enforce", "allow_user_keys": False}
HIGH_RISK_PRODUCTION_FLOOR: Dict[str, Any] = {"min_assurance": "workload_pop"}

#: The level an exception to the workload-identity requirement grants.
EXCEPTION_LEVEL = "bound_key"


class InvalidIdentityPolicy(ValueError):
    """A value that is not a valid identity policy; `field` names the member."""

    def __init__(self, field: str, problem: str):
        super().__init__(f"{field}: {problem}" if field else problem)
        self.field = field
        self.problem = problem


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def level_rank(level: str) -> int:
    return CREDENTIAL_LEVELS.index(level)


def levels_below(level: str) -> List[str]:
    """The levels ranked below `level`, weakest first."""
    return list(CREDENTIAL_LEVELS[: level_rank(level)])


def judged_kind(kind: str) -> bool:
    """Whether an identity policy judges a credential of this kind. A
    person's session and the platform's own credential are outside the
    ladder; anonymous is below every level."""
    return kind in LADDER_KINDS or kind == "anonymous"


# ---------------------------------------------------------------------------
# The writer's rules
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _members(value: Any, allowed: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidIdentityPolicy("", "an identity policy is a JSON object")
    for name in value:
        if name not in allowed:
            raise InvalidIdentityPolicy(str(name), "not a member of an identity policy")

    def member(name: str) -> Any:
        if name not in value:
            raise InvalidIdentityPolicy(name, "missing")
        return value[name]

    mode = member("mode")
    if mode not in POLICY_MODES:
        raise InvalidIdentityPolicy("mode", "must be audit or enforce")
    level = member("min_assurance")
    if level not in CREDENTIAL_LEVELS:
        raise InvalidIdentityPolicy("min_assurance", "must be a credential level")
    allow_user_keys = member("allow_user_keys")
    if not isinstance(allow_user_keys, bool):
        raise InvalidIdentityPolicy("allow_user_keys", "must be a JSON boolean")
    issuers = member("allowed_issuers")
    if issuers is not None:
        if not isinstance(issuers, list) or not all(isinstance(i, str) and i for i in issuers):
            raise InvalidIdentityPolicy("allowed_issuers", "an array of IdP ids, or null")
        if len(set(issuers)) != len(issuers):
            raise InvalidIdentityPolicy("allowed_issuers", "entries must be distinct")
        if not 1 <= len(issuers) <= MAX_ALLOWED_ISSUERS:
            raise InvalidIdentityPolicy("allowed_issuers", "1 to 50 IdP ids, or null")
        issuers = sorted(issuers)
    lifetime = member("max_token_lifetime_s")
    if not _is_int(lifetime) or not LIFETIME_RANGE[0] <= lifetime <= LIFETIME_RANGE[1]:
        raise InvalidIdentityPolicy("max_token_lifetime_s", "an integer from 60 to 3600")
    ttl = member("max_key_ttl_days")
    if ttl is not None and (not _is_int(ttl) or not TTL_DAYS_RANGE[0] <= ttl <= TTL_DAYS_RANGE[1]):
        raise InvalidIdentityPolicy("max_key_ttl_days", "an integer from 1 to 3650, or null")
    return {
        "mode": mode,
        "min_assurance": level,
        "allow_user_keys": allow_user_keys,
        "allowed_issuers": issuers,
        "max_token_lifetime_s": lifetime,
        "max_key_ttl_days": ttl,
    }


def validate_policy(value: Any) -> Dict[str, Any]:
    """A group's identity policy: exactly the six members, each of its type
    and in its range. Returns it normalised (issuers sorted)."""
    return _members(value, POLICY_FIELDS)


def validate_policy_default(value: Any) -> Dict[str, Any]:
    """The tenant default: a policy plus `max_user_key_ttl_days`."""
    policy = _members(value, POLICY_FIELDS + ("max_user_key_ttl_days",))
    ceiling = value.get("max_user_key_ttl_days")
    if not _is_int(ceiling) or not TTL_DAYS_RANGE[0] <= ceiling <= TTL_DAYS_RANGE[1]:
        raise InvalidIdentityPolicy("max_user_key_ttl_days", "an integer from 1 to 3650")
    policy["max_user_key_ttl_days"] = ceiling
    return policy


def _stored_policy(value: Any, owner: str) -> Dict[str, Any]:
    try:
        return validate_policy(value)
    except InvalidIdentityPolicy as invalid:
        logger.error(
            "stored identity policy of %s is invalid (%s); read as the strictest policy", owner, invalid
        )
        return copy.deepcopy(STRICTEST)


def _stored_default(value: Any) -> Dict[str, Any]:
    try:
        return validate_policy_default(value)
    except InvalidIdentityPolicy as invalid:
        logger.error("stored tenant identity default is invalid (%s); read as the strictest policy", invalid)
        return dict(copy.deepcopy(STRICTEST), max_user_key_ttl_days=1)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _source(kind: str, **members: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kind": kind}
    out.update({k: v for k, v in members.items() if v is not None and v is not False})
    return out


def _in_production(node: Mapping[str, Any]) -> bool:
    return bool(node.get("is_ai_system")) and node.get("lifecycle_stage") in PRODUCTION_STAGES


def _high_risk(node: Mapping[str, Any]) -> bool:
    return node.get("risk_tier") in HIGH_RISK_TIERS


def _risk_source(node: Mapping[str, Any]) -> Dict[str, Any]:
    return _source("risk_floor", group_id=node["id"], risk_tier=node.get("risk_tier"))


def _contributions(tenant_default: Optional[Dict[str, Any]], chain: Sequence[Mapping[str, Any]]) -> List[tuple]:
    """(members set, source) in resolution order."""
    out: List[tuple] = [(dict(DEVELOPMENT_DEFAULT), _source("default"))]
    if tenant_default is not None:
        out.append(({f: tenant_default[f] for f in POLICY_FIELDS}, _source("tenant_default")))
    any_high_risk = any(_high_risk(n) for n in chain)
    for node in chain:
        policy = node.get("identity_policy")
        if policy is not None:
            out.append((_stored_policy(policy, f"group {node['id']}"), _source("explicit", group_id=node["id"])))
        if _in_production(node):
            lowered = bool(node.get("gate_exception")) and not any_high_risk
            members = dict(PRODUCTION_FLOOR)
            if lowered:
                members["min_assurance"] = EXCEPTION_LEVEL
            out.append((
                members,
                _source(
                    "lifecycle",
                    group_id=node["id"],
                    stage=node.get("lifecycle_stage"),
                    lowered_by_exception=lowered,
                ),
            ))
        if _high_risk(node):
            out.append((dict(HIGH_RISK_FLOOR), _risk_source(node)))
    first_high = next((n for n in chain if _high_risk(n)), None)
    if first_high is not None and any(_in_production(n) for n in chain):
        out.append((dict(HIGH_RISK_PRODUCTION_FLOOR), _risk_source(first_high)))
    return out


def _stricter_than(field: str, a: Any, b: Any) -> bool:
    """Whether value `a` of `field` is stricter than `b`."""
    if field == "mode":
        return POLICY_MODES.index(a) > POLICY_MODES.index(b)
    if field == "min_assurance":
        return level_rank(a) > level_rank(b)
    if field == "allow_user_keys":
        return (not a) and b
    if field == "max_token_lifetime_s":
        return a < b
    if field == "max_key_ttl_days":
        if a is None:
            return False
        return b is None or a < b
    raise KeyError(field)


def _combine(contributions: List[tuple]) -> tuple:
    effective: Dict[str, Any] = {}
    sources: Dict[str, Dict[str, Any]] = {}
    for field in POLICY_FIELDS:
        if field == "allowed_issuers":
            continue
        best = None
        for members, source in contributions:
            if field not in members:
                continue
            if best is None or _stricter_than(field, members[field], best[0]):
                best = (members[field], source)
        effective[field], sources[field] = best[0], dict(best[1])

    lists = [(m["allowed_issuers"], s) for m, s in contributions if m.get("allowed_issuers") is not None]
    if not lists:
        first_null = next((s for m, s in contributions if "allowed_issuers" in m), _source("default"))
        effective["allowed_issuers"], sources["allowed_issuers"] = None, dict(first_null)
    elif len(lists) == 1:
        effective["allowed_issuers"], sources["allowed_issuers"] = sorted(lists[0][0]), dict(lists[0][1])
    else:
        acc = set(lists[0][0])
        for issuers, _ in lists[1:]:
            acc &= set(issuers)
        effective["allowed_issuers"], sources["allowed_issuers"] = sorted(acc), _source("intersection")

    ordered = {f: effective[f] for f in POLICY_FIELDS}
    return ordered, {f: sources[f] for f in POLICY_FIELDS}


def stricter(a: Mapping[str, Any], b: Mapping[str, Any]) -> Dict[str, Any]:
    """Field by field, the stricter of two policies."""
    out: Dict[str, Any] = {}
    for field in POLICY_FIELDS:
        if field == "allowed_issuers":
            x, y = a[field], b[field]
            if x is None or y is None:
                out[field] = sorted(x if y is None else y) if (x is not None or y is not None) else None
            else:
                out[field] = sorted(set(x) & set(y))
        else:
            out[field] = a[field] if _stricter_than(field, a[field], b[field]) else b[field]
    return out


def looser_fields(own: Mapping[str, Any], floor: Mapping[str, Any]) -> List[str]:
    """The members in which `own` is looser than `floor`, in member order."""
    out: List[str] = []
    for field in POLICY_FIELDS:
        if field == "allowed_issuers":
            if _issuers_looser(own[field], floor[field]):
                out.append(field)
        elif _stricter_than(field, floor[field], own[field]):
            out.append(field)
    return out


def _issuers_looser(a: Optional[Sequence[str]], b: Optional[Sequence[str]]) -> bool:
    """Whether issuer list `a` admits an issuer `b` does not (null = any issuer)."""
    if a is None:
        return b is not None
    return b is not None and not set(a) <= set(b)


def field_changes(before: Any, after: Any) -> List[Tuple[str, str]]:
    """How a group's stored policy moved, field by field in member order:
    `(field, "looser" | "stricter")` per member that differs.

    Each side is read as resolution reads it: no explicit policy contributes
    the development default, an invalid one the strictest policy. An issuer
    list that admits an issuer the other did not is looser, even if it also
    drops one."""
    b = _stored_policy(before, "a contract") if before is not None else DEVELOPMENT_DEFAULT
    a = _stored_policy(after, "a contract") if after is not None else DEVELOPMENT_DEFAULT
    out: List[Tuple[str, str]] = []
    for field in POLICY_FIELDS:
        if field == "allowed_issuers":
            if _issuers_looser(a[field], b[field]):
                out.append((field, "looser"))
            elif _issuers_looser(b[field], a[field]):
                out.append((field, "stricter"))
        elif _stricter_than(field, b[field], a[field]):
            out.append((field, "looser"))
        elif _stricter_than(field, a[field], b[field]):
            out.append((field, "stricter"))
    return out


def resolve(
    *,
    tenant_default: Optional[Mapping[str, Any]],
    chain: Sequence[Mapping[str, Any]],
    attributed_ai_system_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The effective identity policy of a group.

    `chain`: the acting group's ancestors, root first, the acting group
    last; each node `{id, is_ai_system, lifecycle_stage, risk_tier,
    identity_policy, gate_exception?}`. Returns `{effective, sources,
    inherited_floor, put_refused_fields, max_user_key_ttl_days}`.
    """
    default = _stored_default(tenant_default) if tenant_default is not None else None
    effective, sources = _combine(_contributions(default, chain))

    if attributed_ai_system_id is not None:
        at = next((i for i, n in enumerate(chain) if n["id"] == attributed_ai_system_id), None)
        if at is not None:
            cut, _ = _combine(_contributions(default, chain[: at + 1]))
            effective = stricter(effective, cut)

    own = chain[-1].get("identity_policy") if chain else None
    without_own = [dict(n) for n in chain]
    if without_own:
        without_own[-1]["identity_policy"] = None
    inherited_floor, _ = _combine(_contributions(default, without_own))
    refused = looser_fields(_stored_policy(own, f"group {chain[-1]['id']}"), inherited_floor) if own is not None else []

    return {
        "effective": effective,
        "sources": sources,
        "inherited_floor": inherited_floor,
        "put_refused_fields": refused,
        "max_user_key_ttl_days": default["max_user_key_ttl_days"] if default else DEFAULT_MAX_USER_KEY_TTL_DAYS,
    }


# ---------------------------------------------------------------------------
# A group's policy, read from the database
# ---------------------------------------------------------------------------


def is_ai_system(group: Any) -> bool:
    from app.database.models.resource_group import GroupType

    kind = getattr(group, "group_type", None)
    return kind == GroupType.AI_SYSTEM or str(getattr(kind, "value", kind) or "").lower() == "ai_system"


def group_chain(group: Any) -> List[Any]:
    """The group's ancestors, root first, the group last."""
    return group.get_ancestors(include_self=True)


def chain_nodes(groups: Sequence[Any], *, stage_of: Optional[Dict[str, Optional[str]]] = None) -> List[Dict[str, Any]]:
    """Resolver nodes for `groups` (root first). `stage_of` replaces the
    lifecycle stage of the named groups (the gate resolves a system at the
    stage it is moving to). No gate exception exists yet: none is passed."""
    stage_of = stage_of or {}
    return [
        {
            "id": g.id,
            "is_ai_system": is_ai_system(g),
            "lifecycle_stage": stage_of.get(g.id, g.lifecycle_stage),
            "risk_tier": g.eu_ai_act_risk_tier,
            "identity_policy": g.identity_policy,
        }
        for g in groups
    ]


def tenant_default_of(db: Any, tenant_id: str) -> Optional[Dict[str, Any]]:
    from app.database.models.tenant import Tenant

    row = db.query(Tenant.identity_policy_default).filter(Tenant.id == tenant_id).first()
    return row[0] if row else None


def resolve_group(db: Any, group: Any, *, to_stage: Optional[str] = None) -> Dict[str, Any]:
    """The resolution of `group`'s effective policy, with `group_name` on
    every source that names a group. `to_stage` replaces the group's own
    lifecycle stage."""
    groups = group_chain(group)
    stage_of = {group.id: to_stage} if to_stage is not None else None
    resolution = resolve(
        tenant_default=tenant_default_of(db, group.tenant_id),
        chain=chain_nodes(groups, stage_of=stage_of),
    )
    names = {g.id: g.name for g in groups}
    resolution["sources"] = named_sources(resolution["sources"], names)
    resolution["attributed_to_ai_system"] = any(is_ai_system(g) for g in groups)
    return resolution


def resolve_ungrouped(db: Any, tenant_id: str) -> Dict[str, Any]:
    """The policy of a caller no group governs (an empty chain)."""
    resolution = resolve(tenant_default=tenant_default_of(db, tenant_id), chain=[])
    resolution["attributed_to_ai_system"] = False
    return resolution


def named_sources(sources: Mapping[str, Mapping[str, Any]], names: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for field, source in sources.items():
        named = dict(source)
        if "group_id" in named:
            named["group_name"] = names.get(named["group_id"])
        out[field] = named
    return out


def unknown_issuers(db: Any, tenant_id: str, issuers: Optional[Sequence[str]]) -> List[str]:
    """The entries of `issuers` that are not OIDC IdPs of the tenant, sorted."""
    if not issuers:
        return []
    from app.database.models.idp_server import IdPServer

    known = {
        row[0]
        for row in db.query(IdPServer.id)
        .filter(IdPServer.tenant_id == tenant_id, IdPServer.protocol == "oidc", IdPServer.id.in_(list(issuers)))
        .all()
    }
    return sorted(set(issuers) - known)


# ---------------------------------------------------------------------------
# API keys the policy would refuse
# ---------------------------------------------------------------------------


class KeyRefused(Exception):
    """A key mint or edit the effective policy of the key's group refuses."""

    def __init__(self, code: str, message: str, resolution: Dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.message = message
        self.resolution = resolution

    def detail(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "code": self.code,
            "effective_policy": self.resolution["effective"],
            "sources": self.resolution["sources"],
        }


def check_key(
    resolution: Dict[str, Any],
    *,
    personal: bool,
    expires_at: Any,
    creating: bool,
    expiry_changed: bool,
    now: Any,
) -> None:
    """Raise `KeyRefused` when the effective policy refuses this key.

    `creating`: a new key (the level and personal-key checks apply only
    then). `expiry_changed`: the request sets or clears `expires_at` (always
    true for a new key). The platform service key never comes here."""
    from datetime import timedelta, timezone

    effective = resolution["effective"]
    if creating and effective["mode"] == "enforce" and level_rank(effective["min_assurance"]) > level_rank("key"):
        raise KeyRefused(
            "key_refused_by_identity_policy",
            f"The identity policy of this group requires credential level {effective['min_assurance']} "
            "and refuses calls below it; an API key is level key. Use workload identity.",
            resolution,
        )
    if creating and personal and not effective["allow_user_keys"]:
        raise KeyRefused(
            "user_key_not_allowed",
            "The identity policy of this group does not accept personal API keys.",
            resolution,
        )
    if not expiry_changed:
        return
    ceilings = [c for c in (effective["max_key_ttl_days"],) if c is not None]
    if personal:
        ceilings.append(resolution["max_user_key_ttl_days"])
    if not ceilings:
        return
    ceiling = min(ceilings)
    if expires_at is None:
        raise KeyRefused(
            "key_expiry_required",
            f"This key needs an expiry: it may live at most {ceiling} days.",
            resolution,
        )
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > now + timedelta(days=ceiling):
        raise KeyRefused(
            "key_expiry_exceeds_ceiling",
            f"This key may live at most {ceiling} days; the expiry requested is later.",
            resolution,
        )


# ---------------------------------------------------------------------------
# SQL shared by the gate and the identity routes
# ---------------------------------------------------------------------------

#: The CTE the aggregations over an AI System read through: the system and
#: every group below it, in the tenant.
SUBTREE_CTE = """
WITH RECURSIVE subtree AS (
    SELECT id FROM resource_groups WHERE tenant_id = :tenant_id AND id = :ai_system_id
  UNION ALL
    SELECT rg.id FROM resource_groups rg JOIN subtree s ON rg.parent_group_id = s.id
     WHERE rg.tenant_id = :tenant_id
)
"""


def since_clause(db: Any, column: str, days: int) -> Tuple[str, Dict[str, Any]]:
    """`<column> >= <days ago>`, on the database clock where the database
    has one worth the name (Postgres); SQLite (the unit tests) is handed the
    process clock."""
    if db.get_bind().dialect.name == "postgresql":
        return f"{column} >= now() - make_interval(days => :lookback_days)", {"lookback_days": days}
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    # SQLite holds a timestamp as text in this format (SQLAlchemy's).
    return f"{column} >= :since", {"since": since.strftime("%Y-%m-%d %H:%M:%S.%f")}


def now_clause(db: Any) -> Tuple[str, Dict[str, Any]]:
    """The current time in SQL: the database clock on Postgres, the process
    clock (in SQLite's stored format) on SQLite."""
    if db.get_bind().dialect.name == "postgresql":
        return "now()", {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return ":now_ts", {"now_ts": now.strftime("%Y-%m-%d %H:%M:%S.%f")}
