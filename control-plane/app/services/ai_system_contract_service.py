# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Minting, diffing and promoting contracts

## The diff exists to answer one question

Not *"what changed"* — a change list is easy and nearly useless. The
question an approver is actually being asked is:

> *"Does this give the system something it did not have before?"*

So the diff separates **widened** from **narrowed** rather than sorting by
field name. A version that adds three MCP servers and tightens a token cap
is not "four changes"; it is one thing that needs scrutiny and one that
does not. Presenting them as a flat list is how a reviewer learns to click
approve without reading.

## Why the diff is stored

`diff_summary` is persisted on the contract rather than recomputed on
demand. Recomputing it later would diff against *today's* configuration
and show an approver something they never saw — the record of what was
approved has to be immutable, or the audit trail is fiction.

## Promotion is not "save"

Minting a contract is free and happens whenever config changes. Promoting
one to `active` is the governed act: it needs the evidence the tenant's
gate requires, it supersedes exactly one predecessor, and it is written to
the operation log. The database enforces the "one active" half via a
partial unique index, because promotion is a read-then-write and that is
the shape that races.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func
from ulid import ULID
from sqlalchemy.orm import Session

from app.database.models.ai_system_contract import (
    CONTRACT_ACTIVE,
    CONTRACT_DRAFT,
    CONTRACT_REJECTED,
    CONTRACT_SUPERSEDED,
    AISystemContract,
    autonomy_rank,
    hash_closure,
)
from app.database.models.agent_identity import grant_effect_rank
from app.database.models.resource_group import ResourceGroup
from app.services.ai_system_closure import resolve_closure
from app.services.ai_system_outcome_catalogue import (
    diff_catalogues,
    diff_expected_outcomes,
)

# Sections of the closure whose *growth* means the system gained something.
_GRANT_SECTIONS = ("llm_models", "mcp_servers", "skills", "knowledge_bases",
                   "agent_identities", "guardrails", "argument_policies",
                   "cache_configs")


class ContractError(Exception):
    """Raised when a contract operation is refused, with a readable reason."""


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def current_contract(
    db: Session, tenant_id: str, ai_system_id: str
) -> Optional[AISystemContract]:
    return (
        db.query(AISystemContract)
        .filter(
            AISystemContract.tenant_id == tenant_id,
            AISystemContract.ai_system_id == ai_system_id,
            AISystemContract.status == CONTRACT_ACTIVE,
        )
        .first()
    )


def latest_contract(
    db: Session, tenant_id: str, ai_system_id: str
) -> Optional[AISystemContract]:
    return (
        db.query(AISystemContract)
        .filter(
            AISystemContract.tenant_id == tenant_id,
            AISystemContract.ai_system_id == ai_system_id,
        )
        .order_by(AISystemContract.version.desc())
        .first()
    )


def _next_version(db: Session, tenant_id: str, ai_system_id: str) -> int:
    highest = (
        db.query(func.max(AISystemContract.version))
        .filter(
            AISystemContract.tenant_id == tenant_id,
            AISystemContract.ai_system_id == ai_system_id,
        )
        .scalar()
    )
    return int(highest or 0) + 1


def mint(
    db: Session,
    system: ResourceGroup,
    *,
    created_by: Optional[str] = None,
    force: bool = False,
) -> Tuple[AISystemContract, bool]:
    """Resolve current config into a contract.

    Returns `(contract, created)`. If the live configuration still hashes to
    the latest contract, that contract is returned untouched and `created`
    is `False` — minting an identical version on every save would bury the
    real changes in a version history nobody can read.

    `force=True` mints anyway, producing a version whose diff is empty. That
    is the re-attestation case and the *signature* is the change: an approver
    can only sign a draft (`approve` refuses anything else), so a new owner
    who wants their own name against unchanged config has no other route.
    Rare and deliberate — hence opt-in rather than the default.
    """
    closure = resolve_closure(db, system)
    contract_hash = hash_closure(closure)

    latest = latest_contract(db, system.tenant_id, system.id)
    if latest is not None and latest.contract_hash == contract_hash and not force:
        return latest, False

    version = _next_version(db, system.tenant_id, system.id)
    summary = (
        diff_closures(
            latest.resolved_closure,
            closure,
            resolve_contract=contract_closure_resolver(db, system.tenant_id),
        )
        if latest is not None
        else None
    )

    contract = AISystemContract(
        id=f"ctr-{ULID()}",
        tenant_id=system.tenant_id,
        ai_system_id=system.id,
        version=version,
        contract_hash=contract_hash,
        resolved_closure=closure,
        status=CONTRACT_DRAFT,
        diff_from_version=latest.version if latest is not None else None,
        diff_summary=summary,
        created_by=created_by,
    )
    db.add(contract)
    db.flush()
    return contract, True


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


#: `contract id -> its resolved_closure | None`. Supplied by the caller so
#: the diff stays a pure function of its inputs.
ContractResolver = Callable[[str], Optional[Dict[str, Any]]]


def contract_closure_resolver(db: Session, tenant_id: str) -> ContractResolver:
    """Look a contract's frozen closure up by id, within one tenant."""

    def resolve(contract_id: str) -> Optional[Dict[str, Any]]:
        row = (
            db.query(AISystemContract)
            .filter(
                AISystemContract.tenant_id == tenant_id,
                AISystemContract.id == contract_id,
            )
            .first()
        )
        return row.resolved_closure if row is not None else None

    return resolve


def diff_closures(
    before: Dict[str, Any],
    after: Dict[str, Any],
    *,
    resolve_contract: Optional[ContractResolver] = None,
) -> Dict[str, Any]:
    """Structured diff, split by direction rather than by field.

    `widened` is the list an approver has to read. `narrowed` and
    `changed` are context.

    `resolve_contract` lets a dependency's contract change be shown as the
    SERVICE's own classified diff inside this one: without
    it, a moved dependency contract is reported as widened with a pointer
    to read the service's contract — never as silently fine.
    """
    widened: List[Dict[str, Any]] = []
    narrowed: List[Dict[str, Any]] = []
    changed: List[Dict[str, Any]] = []

    _diff_composition(before, after, widened, narrowed)
    _diff_content_changes(before, after, widened)
    _diff_response_policies(before, after, widened, narrowed)
    _diff_autonomy(before, after, widened, narrowed)
    _diff_limit_tree(
        before.get("limits") or {}, after.get("limits") or {}, "limits",
        widened, narrowed,
    )
    _diff_limit_tree(
        before.get("governance") or {}, after.get("governance") or {}, "governance",
        widened, narrowed,
    )
    _diff_envelope(before, after, widened, narrowed)
    diff_catalogues(before.get("outcomes"), after.get("outcomes"), widened, narrowed, changed)
    _diff_agent_grants(before, after, widened, narrowed)
    _diff_capability_gates(before, after, widened, narrowed)
    _diff_skill_approval_defaults(before, after, widened, narrowed)
    _diff_agent_implementation(before, after, widened, narrowed)
    _diff_dependencies(before, after, widened, narrowed, changed, resolve_contract)
    _diff_caller_enforcement(before, after, widened, narrowed)
    _diff_identity_policy(before, after, widened, narrowed, changed)
    _diff_attributes(before, after, changed)

    return {
        "widened": widened,
        "narrowed": narrowed,
        "changed": changed,
        "widened_count": len(widened),
        "narrowed_count": len(narrowed),
        "changed_count": len(changed),
        # The single fact a promotion gate keys on.
        "has_widened_permissions": bool(widened),
    }


def _diff_composition(before, after, widened, narrowed) -> None:
    b_comp = before.get("composition") or {}
    a_comp = after.get("composition") or {}
    for section in _GRANT_SECTIONS:
        b_ids = _ids(b_comp.get(section))
        a_ids = _ids(a_comp.get(section))
        for added in sorted(a_ids - b_ids):
            widened.append(
                {
                    "kind": "resource_added",
                    "section": section,
                    "id": added,
                    "detail": f"{section} gained {added}",
                }
            )
        for removed in sorted(b_ids - a_ids):
            narrowed.append(
                {
                    "kind": "resource_removed",
                    "section": section,
                    "id": removed,
                    "detail": f"{section} lost {removed}",
                }
            )


def _diff_content_changes(before, after, widened) -> None:
    """Same id, different content — the edit an id-based diff cannot see.

    Reported as *widened* deliberately, even though the edit might be a
    tightening: the approver cannot know which without reading it, and a
    guardrail whose rules changed under an unchanged id is exactly the entry
    that must not be summarised as "grants nothing new".
    """
    b_comp = before.get("composition") or {}
    a_comp = after.get("composition") or {}
    for section in ("guardrails", "argument_policies", "cache_configs"):
        b_by_id = {
            str(i.get("id")): i.get("content_sha256")
            for i in (b_comp.get(section) or [])
            if isinstance(i, dict)
        }
        for item in a_comp.get(section) or []:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id"))
            if iid in b_by_id and b_by_id[iid] != item.get("content_sha256"):
                widened.append(
                    {
                        "kind": "content_changed",
                        "section": section,
                        "id": iid,
                        "detail": (
                            f"{section} {item.get('name') or iid}: configuration "
                            f"content changed (same binding, different rules) — "
                            f"read the config before approving"
                        ),
                    }
                )


def _diff_response_policies(before, after, widened, narrowed) -> None:
    """Response policies are part of the contract.

    An armed policy that can restrict the system unattended is context
    (narrowing-flavoured); *removing or disabling* one is the widening: the
    system loses an automatic safety response nobody may notice is gone.
    """
    def by_id(closure):
        return {
            str(p.get("id")): p
            for p in (closure.get("response_policies") or [])
            if isinstance(p, dict)
        }

    b, a = by_id(before), by_id(after)
    for pid in sorted(a.keys() - b.keys()):
        narrowed.append(
            {
                "kind": "response_policy_added",
                "section": "response_policies",
                "id": pid,
                "detail": f"response policy {a[pid].get('name') or pid} attached",
            }
        )
    for pid in sorted(b.keys() - a.keys()):
        widened.append(
            {
                "kind": "response_policy_removed",
                "section": "response_policies",
                "id": pid,
                "detail": (
                    f"response policy {b[pid].get('name') or pid} removed — the "
                    f"system loses an automatic response"
                ),
            }
        )
    for pid in sorted(b.keys() & a.keys()):
        was, now = bool(b[pid].get("enabled")), bool(a[pid].get("enabled"))
        if was and not now:
            widened.append(
                {
                    "kind": "response_policy_disabled",
                    "section": "response_policies",
                    "id": pid,
                    "detail": (
                        f"response policy {b[pid].get('name') or pid} disabled — "
                        f"it will no longer fire"
                    ),
                }
            )
        elif b[pid] != a[pid]:
            narrowed.append(
                {
                    "kind": "response_policy_changed",
                    "section": "response_policies",
                    "id": pid,
                    "detail": f"response policy {a[pid].get('name') or pid} changed",
                }
            )


def _ids(section: Any) -> set:
    if not section:
        return set()
    if isinstance(section, list) and section and isinstance(section[0], dict):
        return {str(item.get("id")) for item in section}
    if isinstance(section, list):
        return {str(item) for item in section}
    return set()


def _diff_envelope(before, after, widened, narrowed) -> None:
    """Operating-envelope terms, classified by which way they moved.

    Direction-aware: raising a `max_*` bound or lowering a `min_*` bound
    loosens what the system promised, and a **removed** term removes a
    promise entirely — both are widenings the approver has to read.
    Adding or tightening a term is a narrowing.
    """
    from app.services.ai_system_envelope import term_label, term_spec

    b_env = before.get("operating_envelope") or {}
    a_env = after.get("operating_envelope") or {}

    for term in sorted(set(b_env) | set(a_env)):
        spec = term_spec(term)
        if spec is None:
            continue  # a term from a newer vocabulary than this code
        b_val, a_val = b_env.get(term), a_env.get(term)
        label = term_label(term)
        if b_val is None:
            narrowed.append({
                "kind": "envelope_term_added",
                "section": "operating_envelope",
                "id": term,
                "detail": f"envelope now bounds {label.lower()} at {a_val}",
            })
            continue
        if a_val is None:
            widened.append({
                "kind": "envelope_term_removed",
                "section": "operating_envelope",
                "id": term,
                "detail": (
                    f"envelope no longer bounds {label.lower()} "
                    f"(was {b_val}) — a removed promise, not a tidy-up"
                ),
            })
            continue
        if a_val == b_val:
            continue
        loosened = (a_val > b_val) if spec["direction"] == "max" else (a_val < b_val)
        entry = {
            "kind": "envelope_term_loosened" if loosened else "envelope_term_tightened",
            "section": "operating_envelope",
            "id": term,
            "detail": (
                f"{label} bound moved {b_val} → {a_val} "
                f"({'loosened' if loosened else 'tightened'})"
            ),
        }
        (widened if loosened else narrowed).append(entry)


def _diff_autonomy(before, after, widened, narrowed) -> None:
    b = before.get("autonomy_level")
    a = after.get("autonomy_level")
    if b == a:
        return
    entry = {
        "kind": "autonomy_level",
        "from": b,
        "to": a,
        "detail": f"autonomy {b} → {a}",
    }
    # Higher is more permissive; a level the gateway does not know is suspended.
    if autonomy_rank(a) > autonomy_rank(b):
        widened.append(entry)
    else:
        narrowed.append(entry)


def _diff_limit_tree(before, after, prefix, widened, narrowed) -> None:
    """A *raised* cap is a widening; a lowered one is a narrowing.

    Removing a cap entirely is the widest change of all and is reported as
    such rather than as a deletion, because "no limit" reads as harmless in
    a plain change list and is anything but.
    """
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        path = f"{prefix}.{key}"
        if isinstance(b, dict) or isinstance(a, dict):
            _diff_limit_tree(
                b if isinstance(b, dict) else {},
                a if isinstance(a, dict) else {},
                path, widened, narrowed,
            )
            continue
        if b == a:
            continue
        entry = {"kind": "limit", "path": path, "from": b, "to": a}
        if a is None and b is not None:
            entry["detail"] = f"{path} cap removed (was {b})"
            widened.append(entry)
        elif b is None and a is not None:
            entry["detail"] = f"{path} cap added ({a})"
            narrowed.append(entry)
        elif isinstance(b, bool) or isinstance(a, bool):
            entry["detail"] = f"{path} {b} → {a}"
            (narrowed if a else widened).append(entry)
        elif isinstance(b, (int, float)) and isinstance(a, (int, float)):
            entry["detail"] = f"{path} {b} → {a}"
            (widened if a > b else narrowed).append(entry)
        else:
            entry["detail"] = f"{path} {b!r} → {a!r}"
            widened.append(entry)


#: `lifecycle_stage` is deliberately absent: it left the closure in schema
#: version 2, because promoting a contract is what changes it.
_ATTRIBUTE_KEYS = (
    "system_kind", "owner", "intended_use",
    "intended_client_labels", "intended_client_systems",
    "eu_ai_act_risk_tier", "phi_resource",
)


def _diff_capability_gates(before, after, widened, narrowed) -> None:
    """A gate removed takes a person out of the loop: always `widened`, the
    same fact to a risk committee as a grant relaxed to `allow`."""

    def index(closure):
        return {
            (g.get("server_config_id"), g.get("capability_type"), g.get("capability_name"))
            for g in (closure.get("mcp_capability_gates") or [])
        }

    b, a = index(before), index(after)
    for server, kind, name in sorted(b - a):
        widened.append({
            "kind": "mcp_capability_gate", "server_config_id": server, "capability": name,
            "detail": f"{kind} {name} no longer needs human approval",
        })
    for server, kind, name in sorted(a - b):
        narrowed.append({
            "kind": "mcp_capability_gate", "server_config_id": server, "capability": name,
            "detail": f"{kind} {name} now needs human approval",
        })


def _diff_skill_approval_defaults(before, after, widened, narrowed) -> None:
    """A skill's own approval default, for a skill bound on both sides (adding
    or removing the skill itself is `_diff_composition`'s finding)."""

    def index(closure):
        return {
            s.get("id"): s
            for s in ((closure.get("composition") or {}).get("skills") or [])
        }

    b, a = index(before), index(after)
    for skill_id in sorted(set(b) & set(a)):
        old, new = bool(b[skill_id].get("approval_required")), bool(a[skill_id].get("approval_required"))
        if old == new:
            continue
        name = a[skill_id].get("name") or skill_id
        bucket, detail = (
            (widened, f"skill {name} no longer needs human approval by default")
            if old
            else (narrowed, f"skill {name} now needs human approval by default")
        )
        bucket.append({"kind": "skill_approval_default", "skill": skill_id, "detail": detail})


def _diff_agent_grants(before, after, widened, narrowed) -> None:
    """Grant changes, classified by whether the agent can now do more.

    Keyed on (agent, action_type, target) so an effect change on one grant
    reads as a change rather than as an unrelated add plus remove. A new
    `allow` and an existing grant relaxed from `approval_required` to
    `allow` are the same fact to a risk committee, so both land in
    `widened`.
    """

    def index(closure):
        return {
            (g.get("agent_id"), g.get("action_type"), g.get("target")): g
            for g in (closure.get("agent_grants") or [])
        }

    b, a = index(before), index(after)
    for key in sorted(set(b) | set(a), key=lambda k: tuple(str(p) for p in k)):
        agent, action, target = key
        old, new = b.get(key), a.get(key)
        label = f"{action} → {target}"
        name = (new or old).get("agent_name") or agent
        if old is None:
            bucket = widened if grant_effect_rank(new.get("effect")) > 0 else narrowed
            bucket.append({
                "kind": "agent_grant", "agent": name, "grant": label,
                "detail": f"granted {new.get('effect')}",
            })
        elif new is None:
            narrowed.append({
                "kind": "agent_grant", "agent": name, "grant": label,
                "detail": f"revoked {old.get('effect')}",
            })
        else:
            # A move up `GRANT_EFFECT_RANK` is a widened permission and is
            # the thing an approver must not miss.
            o_rank = grant_effect_rank(old.get("effect"))
            n_rank = grant_effect_rank(new.get("effect"))
            if n_rank > o_rank:
                widened.append({
                    "kind": "agent_grant", "agent": name, "grant": label,
                    "detail": f"{old.get('effect')} → {new.get('effect')}",
                })
            elif n_rank < o_rank:
                narrowed.append({
                    "kind": "agent_grant", "agent": name, "grant": label,
                    "detail": f"{old.get('effect')} → {new.get('effect')}",
                })
            elif old.get("constraints") != new.get("constraints"):
                # Constraints only ever restrict, so a change here cannot
                # widen the grant — but it is not silent either.
                narrowed.append({
                    "kind": "agent_grant", "agent": name, "grant": label,
                    "detail": "constraints changed",
                })


def _diff_agent_implementation(before, after, widened, narrowed) -> None:
    """The implementation declaration of each member
    agent, classified by whether the system may now run more software.

    * an approved version pattern ADDED widens (a release that was a finding
      is now accepted); one REMOVED narrows. Going from none to some is the
      approval starting to bite (narrowed); going from some to none returns
      the identity to observe-only (widened) — the per-pattern entries would
      read backwards there, so those two are one entry each.
    * `require_release` turned on narrows, off widens.
    * `implementation_name` declared narrows, cleared widens, renamed is a
      change an approver must read (widened: other software is now accepted).

    Agents added or removed as members are `_diff_composition`'s; only
    agents present on both sides are compared here.
    """

    def index(closure):
        return {
            a.get("id"): a
            for a in ((closure.get("composition") or {}).get("agent_identities") or [])
            if isinstance(a, dict)
        }

    b, a = index(before), index(after)
    for agent_id in sorted(set(b) & set(a), key=str):
        old, new = b[agent_id], a[agent_id]
        name = new.get("name") or old.get("name") or agent_id
        base = {"kind": "agent_implementation", "agent": name, "agent_id": agent_id}

        o_ver = set(old.get("approved_versions") or [])
        n_ver = set(new.get("approved_versions") or [])
        if o_ver != n_ver:
            if not o_ver:
                narrowed.append({**base, "field": "approved_versions", "detail":
                                 f"releases now need approval: {', '.join(sorted(n_ver))}"})
            elif not n_ver:
                widened.append({**base, "field": "approved_versions", "detail":
                                "every release is accepted again (observe only)"})
            else:
                for v in sorted(n_ver - o_ver):
                    widened.append({**base, "field": "approved_versions", "version": v,
                                    "detail": f"release {v} approved"})
                for v in sorted(o_ver - n_ver):
                    narrowed.append({**base, "field": "approved_versions", "version": v,
                                     "detail": f"release {v} no longer approved"})

        o_req, n_req = bool(old.get("require_release")), bool(new.get("require_release"))
        if o_req != n_req:
            (narrowed if n_req else widened).append({
                **base, "field": "require_release", "from": o_req, "to": n_req,
                "detail": (
                    "runs without a declared release are now a finding" if n_req
                    else "runs without a declared release are no longer a finding"
                ),
            })

        o_name, n_name = old.get("implementation_name"), new.get("implementation_name")
        if o_name != n_name:
            entry = {**base, "field": "implementation_name", "from": o_name, "to": n_name}
            if o_name is None:
                narrowed.append({**entry, "detail": f"must now declare {n_name!r}"})
            elif n_name is None:
                widened.append({**entry, "detail": f"no longer required to declare {o_name!r}"})
            else:
                widened.append({**entry, "detail":
                                f"accepted implementation {o_name!r} → {n_name!r}"})


#: How many of a dependency's own diff entries are spelled out in the
#: caller's entry detail; the full lists travel in `dependency_diff`.
_DEPENDENCY_DETAIL_ENTRIES = 5


def _dependency_key(d: Dict[str, Any]) -> tuple:
    return (d.get("role") or "", d.get("depends_on_system_id") or "", d.get("agent_card_id") or "")


def _dependency_label(d: Dict[str, Any], names: Dict[str, str]) -> str:
    target = d.get("depends_on_system_id")
    if target:
        return names.get(target) or target
    return f"agent card {d.get('agent_card_id')}"


def _diff_dependencies(before, after, widened, narrowed, changed, resolve_contract) -> None:
    """Dependency edges, and the contracts they run against.

    Adding an edge is a widening (the system now relies on, and may pass
    data to, something new); removing one narrows. Declaring an edge
    non-contributing or degradable weakens what the approver can rely on,
    so it widens. A FOLLOWED service's contract moving is reported as the
    service's own classified diff: "dependency X widened: …".
    """
    b = {_dependency_key(d): d for d in before.get("dependencies") or [] if isinstance(d, dict)}
    a = {_dependency_key(d): d for d in after.get("dependencies") or [] if isinstance(d, dict)}
    if not b and not a:
        return

    # Names come from the services' own frozen contracts when available.
    names: Dict[str, str] = {}
    closures: Dict[str, Optional[Dict[str, Any]]] = {}

    def contract_closure(ref: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not ref or not ref.get("id") or resolve_contract is None:
            return None
        cid = ref["id"]
        if cid not in closures:
            closures[cid] = resolve_contract(cid)
            sys_blob = (closures[cid] or {}).get("ai_system") or {}
            if sys_blob.get("id") and sys_blob.get("name"):
                names.setdefault(sys_blob["id"], sys_blob["name"])
        return closures[cid]

    for d in list(b.values()) + list(a.values()):
        contract_closure(d.get("contract"))

    for key in sorted(a.keys() - b.keys()):
        d = a[key]
        label = _dependency_label(d, names)
        widened.append({
            "kind": "dependency_added",
            "section": "dependencies",
            "id": d.get("depends_on_system_id") or d.get("agent_card_id"),
            "detail": (
                f"now depends on {label} ({d.get('role')}, {d.get('criticality')}"
                f"{', contributes to decisions' if d.get('contributes_to_decision') else ''})"
            ),
        })
    for key in sorted(b.keys() - a.keys()):
        d = b[key]
        narrowed.append({
            "kind": "dependency_removed",
            "section": "dependencies",
            "id": d.get("depends_on_system_id") or d.get("agent_card_id"),
            "detail": f"no longer depends on {_dependency_label(d, names)}",
        })

    for key in sorted(b.keys() & a.keys()):
        old, new = b[key], a[key]
        label = _dependency_label(new, names)
        dep_id = new.get("depends_on_system_id") or new.get("agent_card_id")

        def entry(kind: str, detail: str, **extra) -> Dict[str, Any]:
            return {"kind": kind, "section": "dependencies", "id": dep_id,
                    "detail": detail, **extra}

        if old.get("contributes_to_decision") and not new.get("contributes_to_decision"):
            widened.append(entry(
                "dependency_separability_declared",
                f"{label} declared NOT to contribute to decisions — read the "
                f"separability rationale before approving",
            ))
        elif not old.get("contributes_to_decision") and new.get("contributes_to_decision"):
            narrowed.append(entry(
                "dependency_contribution_declared", f"{label} now declared to contribute to decisions",
            ))
        if old.get("criticality") != new.get("criticality"):
            if new.get("criticality") == "degradable":
                widened.append(entry(
                    "dependency_criticality",
                    f"{label} now degradable (was {old.get('criticality')}) — the system "
                    f"claims it can operate without it; read the fallback",
                ))
            else:
                narrowed.append(entry(
                    "dependency_criticality",
                    f"{label} now {new.get('criticality')} (was {old.get('criticality')})",
                ))
        for term in ("fallback", "separability_rationale", "assumptions", "supplier_legal_entity"):
            if (old.get(term) or None) != (new.get(term) or None):
                changed.append(entry(
                    "dependency_terms", f"{label}: {term.replace('_', ' ')} changed",
                    field=term,
                ))
        diff_expected_outcomes(
            old.get("expected_outcomes"), new.get("expected_outcomes"), label, entry,
            widened, narrowed,
        )
        if old.get("contract_pin") != new.get("contract_pin"):
            to = new.get("contract") or {}
            changed.append(entry(
                "dependency_pin",
                (
                    f"{label} pinned to contract v{to.get('version')}"
                    if new.get("contract_pin")
                    else f"{label} now follows its active contract"
                ),
            ))
        _diff_dependency_contract(
            old, new, label, entry, widened, narrowed, changed, contract_closure,
        )


def _diff_dependency_contract(old, new, label, entry, widened, narrowed, changed,
                              contract_closure) -> None:
    b_ref, a_ref = old.get("contract"), new.get("contract")
    if b_ref == a_ref:
        return
    if a_ref is None:
        widened.append(entry(
            "dependency_contract_changed",
            f"{label} no longer has a contract to run against — it is unapproved "
            f"from this system's point of view",
        ))
        return
    if b_ref is None:
        narrowed.append(entry(
            "dependency_contract_changed",
            f"{label} now runs under contract v{a_ref.get('version')}",
            to_version=a_ref.get("version"),
        ))
        return
    versions = f"v{b_ref.get('version')} → v{a_ref.get('version')}"
    if b_ref.get("hash") == a_ref.get("hash"):
        changed.append(entry(
            "dependency_contract_changed",
            f"{label} re-attested ({versions}); its terms did not change",
            from_version=b_ref.get("version"), to_version=a_ref.get("version"),
        ))
        return
    b_closure, a_closure = contract_closure(b_ref), contract_closure(a_ref)
    if b_closure is None or a_closure is None:
        widened.append(entry(
            "dependency_contract_changed",
            f"{label} changed contract ({versions}) — its diff could not be "
            f"resolved; read the service's contract before approving",
            from_version=b_ref.get("version"), to_version=a_ref.get("version"),
            dependency_diff=None,
        ))
        return
    # One level deep: the service's own diff, not its dependencies' — those
    # are the service approver's to read, and following them would loop on
    # a dependency cycle.
    nested = diff_closures(b_closure, a_closure)
    own = {k: nested[k] for k in ("widened", "narrowed", "changed")}

    def spelled(items: List[Dict[str, Any]]) -> str:
        shown = "; ".join(str(i.get("detail")) for i in items[:_DEPENDENCY_DETAIL_ENTRIES])
        extra = len(items) - _DEPENDENCY_DETAIL_ENTRIES
        return shown + (f"; …and {extra} more" if extra > 0 else "")

    extra = dict(
        from_version=b_ref.get("version"), to_version=a_ref.get("version"),
        dependency_diff=own,
    )
    if nested["widened"]:
        widened.append(entry(
            "dependency_contract_changed",
            f"dependency {label} widened ({versions}): {spelled(nested['widened'])}",
            **extra,
        ))
    elif nested["narrowed"]:
        narrowed.append(entry(
            "dependency_contract_changed",
            f"dependency {label} narrowed ({versions}): {spelled(nested['narrowed'])}",
            **extra,
        ))
    else:
        changed.append(entry(
            "dependency_contract_changed",
            f"dependency {label} changed ({versions}): "
            f"{spelled(nested['changed']) or 'no classified change'}",
            **extra,
        ))


def _diff_caller_enforcement(before, after, widened, narrowed) -> None:
    """A service that starts REFUSING callers outside its
    intended clients narrows who can reach it; one that stops lets any
    caller with a grant through again, which widens."""
    b = bool((before.get("ai_system") or {}).get("enforce_accepted_callers"))
    a = bool((after.get("ai_system") or {}).get("enforce_accepted_callers"))
    if a == b:
        return
    entry = {"kind": "caller_enforcement", "from": b, "to": a}
    if a:
        narrowed.append({**entry, "detail": "callers outside intended_client_systems are now refused"})
    else:
        widened.append({
            **entry,
            "detail": "callers outside intended_client_systems are no longer refused (advisory only)",
        })


def _diff_identity_policy(before, after, widened, narrowed, changed) -> None:
    """The system's stored identity policy, compared field by field the way
    resolution compares contributions. A looser field admits credentials
    the approved contract refused, so it is a widening; a stricter one a
    narrowing. Setting or removing a policy that equals the default moves
    no field and is reported as a change."""
    from app.services.identity_policy import DEVELOPMENT_DEFAULT, field_changes

    b = before.get("identity_policy")
    a = after.get("identity_policy")
    if a == b:
        return
    moves = field_changes(b, a)
    for field, direction in moves:
        # No explicit policy reads as the default, as in resolution.
        b_val = (DEVELOPMENT_DEFAULT if b is None else b).get(field)
        a_val = (DEVELOPMENT_DEFAULT if a is None else a).get(field)
        entry = {
            "kind": "identity_policy",
            "field": field,
            "from": b_val,
            "to": a_val,
            "detail": f"identity policy {field}: {b_val!r} → {a_val!r} ({direction})",
        }
        (widened if direction == "looser" else narrowed).append(entry)
    if not moves:
        changed.append({
            "kind": "identity_policy",
            "field": "policy",
            "from": b,
            "to": a,
            "detail": "explicit identity policy " + ("removed" if a is None else "set")
            + " (same as the default)",
        })


def _diff_attributes(before, after, changed) -> None:
    b = before.get("ai_system") or {}
    a = after.get("ai_system") or {}
    for key in _ATTRIBUTE_KEYS:
        if b.get(key) != a.get(key):
            changed.append(
                {
                    "kind": "attribute",
                    "field": key,
                    "from": b.get(key),
                    "to": a.get(key),
                    "detail": f"{key} {b.get(key)!r} → {a.get(key)!r}",
                }
            )


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def approve(
    db: Session, contract: AISystemContract, *, approved_by: str
) -> AISystemContract:
    if contract.status not in (CONTRACT_DRAFT,):
        raise ContractError(
            f"only a draft contract can be approved; v{contract.version} is "
            f"{contract.status}"
        )
    contract.approved_by = approved_by
    contract.approved_at = datetime.now(timezone.utc)
    db.flush()
    return contract


def reject(
    db: Session, contract: AISystemContract, *, reason: str
) -> AISystemContract:
    if contract.status != CONTRACT_DRAFT:
        raise ContractError(
            f"only a draft contract can be rejected; v{contract.version} is "
            f"{contract.status}"
        )
    contract.status = CONTRACT_REJECTED
    contract.rejection_reason = reason
    db.flush()
    return contract


def promote(
    db: Session,
    contract: AISystemContract,
    *,
    lifecycle_stage: Optional[str] = None,
) -> AISystemContract:
    """Make this contract the one governing runs.

    Supersedes the current active contract rather than deleting it: the
    superseded row is the only evidence of what the rules were while
    historical runs executed.
    """
    if contract.status != CONTRACT_DRAFT:
        raise ContractError(
            f"only a draft contract can be promoted; v{contract.version} is "
            f"{contract.status}"
        )
    # The active contract is the one that governs runs, and the question a
    # reviewer asks of it is "who signed this?". Promoting an unapproved
    # draft would make the answer "nobody" — the lifecycle gate checks
    # `approver_recorded` on the *latest* contract, but promotion is its
    # own write path and used to accept any draft.
    if not contract.is_approved:
        raise ContractError(
            f"contract v{contract.version} has not been approved; record the "
            f"approver with POST /v1/admin/contracts/{contract.id}/approve "
            "before promoting it"
        )

    active = current_contract(db, contract.tenant_id, contract.ai_system_id)
    if active is not None and active.id != contract.id:
        active.status = CONTRACT_SUPERSEDED
        # Flushed before the new row claims `active`, or the partial unique
        # index rejects the promotion — two rows would briefly both be
        # active within the statement.
        db.flush()

    contract.status = CONTRACT_ACTIVE
    contract.promoted_at = datetime.now(timezone.utc)
    contract.lifecycle_stage_at_promotion = lifecycle_stage
    if active is not None and active.id != contract.id:
        catalogue_change_epochs(db, contract, previous=active)
    db.flush()
    return contract


def catalogue_change_epochs(
    db: Session, contract: AISystemContract, *, previous: Optional[AISystemContract]
) -> List[Any]:
    """The promoted contract changed what an
    outcome bucket MEANS (a variant's merged `expect`, or its outcome's
    `counts_toward_baseline`): start a `catalogue_change` baseline epoch on
    that bucket for every per-outcome metric, at the promotion time, so the
    bucket's baselines re-learn rather than judge the new meaning against
    the old normal. A changed baseline flag also changes the population of
    `outcome_mix`, which re-learns on bucket 'all'.

    Promotion happens once per contract (a promoted contract is no longer a
    draft), so each key is written once. Returns the epochs written. No drift event and no seal: the
    authority is the approved contract itself."""
    from app.constants.drift_resolution import (
        EPOCH_ALL_BUCKETS,
        EPOCH_ALL_CALLERS,
        KIND_CATALOGUE_CHANGE,
    )
    from app.database.models.ai_system_drift import BASIS_RUNS
    from app.database.models.ai_system_drift_resolution import AISystemBaselineEpoch
    from app.services.ai_system_outcome_catalogue import (
        BUCKETED_METRICS,
        OUTCOME_MIX_METRIC,
        changed_buckets,
    )

    if previous is None:
        return []
    buckets, flag_changed = changed_buckets(
        (previous.resolved_closure or {}).get("outcomes"),
        (contract.resolved_closure or {}).get("outcomes"),
    )
    keys = [(metric, bucket) for bucket in buckets for metric in BUCKETED_METRICS]
    if flag_changed:
        keys.append((OUTCOME_MIX_METRIC, EPOCH_ALL_BUCKETS))
    starts_at = contract.promoted_at or datetime.now(timezone.utc)
    written = []
    for metric, bucket in keys:
        epoch = AISystemBaselineEpoch(
            id=f"epoch-{ULID()}",
            tenant_id=contract.tenant_id,
            ai_system_id=contract.ai_system_id,
            metric_key=metric,
            basis=BASIS_RUNS,
            caller_system_id=EPOCH_ALL_CALLERS,
            bucket=bucket,
            starts_at=starts_at,
            kind=KIND_CATALOGUE_CHANGE,
            reason=(
                f"Contract v{contract.version} changed the meaning of outcome bucket "
                f"{bucket!r} (expect or baseline flag); its baselines re-learn from here."
                if bucket != EPOCH_ALL_BUCKETS
                else f"Contract v{contract.version} changed which outcomes count toward "
                "the baseline; the outcome mix re-learns from here."
            ),
        )
        db.add(epoch)
        written.append(epoch)
    return written


def is_config_drifted(db: Session, system: ResourceGroup) -> bool:
    """Whether live config no longer matches the active contract.

    This is the declared-vs-observed conformance question, and the reason contracts
    are generated rather than authored: the answer is computable.
    """
    active = current_contract(db, system.tenant_id, system.id)
    if active is None:
        return False
    return not active.matches(resolve_closure(db, system))


__all__ = [
    "ContractError",
    "ContractResolver",
    "approve",
    "catalogue_change_epochs",
    "contract_closure_resolver",
    "current_contract",
    "diff_closures",
    "is_config_drifted",
    "latest_contract",
    "mint",
    "promote",
    "reject",
]
