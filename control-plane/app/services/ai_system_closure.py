# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Resolving an AI System's closure

The contract is a **serialization of an existing computation**, not new
policy logic. An AI System is a resource group (Design Note 002), so "everything
that governs this system" is the resolved closure of that group, and both
composition rules already exist in the platform:

  * **Resources compose additively up the parent chain, gated by
    `inherit_from_parent`.** The walk stops at the first group that does
    not inherit — that group's own bindings still count, its ancestors' do
    not. This mirrors `resolve_effective_groups` in
    `brutor-gateway-core/src/routes/portal_common.rs`, including its
    10-level bound, because a contract that describes different resources
    from the ones the proxy will actually grant is worse than no contract.

  * **Limits and governance compose restrictively, min-wins.** A parent
    cannot be loosened by a child.

## Why this module produces a plain dict

The output is hashed. Every value in it therefore has to be JSON-native,
ordered deterministically, and free of anything incidental — timestamps of
when the resolution ran, database row ids that change on reseed, Python
set ordering. Two resolutions of an unchanged configuration must produce
byte-identical output or the hash means nothing and every sweep looks like
a config change.

That is also why nothing here is a Pydantic model: Pydantic strips
undeclared fields, and a field silently dropped from a *contract* is a
control that quietly stops being on the record.

## What is deliberately excluded

  * **Secrets and credentials.** A contract is shown to auditors. It names
    which API key binding applies, never the key.
  * **Anything mutable that is not a control** — descriptions, icons,
    colours, `updated_at`. Including them would make a cosmetic edit
    produce a new contract version and a "config changed" alert, which is
    the fastest way to teach an operator to ignore contract diffs.
"""

from __future__ import annotations

import copy
import hashlib
import json

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.database.models.agent_identity import (
    AgentGrant,
    AgentIdentity,
    ResourceGroupAgentMember,
)
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.kb_collection import KbCollection, resource_group_kb_collections
from app.database.models.ai_system_contract import (
    DEFAULT_AUTONOMY_LEVEL,
    hash_closure,
    resolve_autonomy,
)
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig

#: Bump when the *shape* changes in a way that makes old hashes
#: incomparable. Stored inside the closure so a contract minted by an older
#: version is self-describing rather than silently mis-diffed.
#: Bumped to 2 when `lifecycle_stage` was removed from the closure. A
#: contract is a snapshot of the *controls* that govern a system; the
#: lifecycle stage is the system's position in its own promotion workflow,
#: and promoting a contract is exactly what advances it. Including it meant
#: every contract invalidated itself the moment it was promoted — the live
#: closure no longer matched the one that had just been signed, so every
#: system reported "running configuration nobody approved" forever. The
#: stage at promotion is still recorded, on `contracts.lifecycle_stage_at_promotion`.
#: Bumped to 3 when `intended_clients` became the typed pair
#: `intended_client_labels` / `intended_client_systems`. Renaming a hashed key
#: changes every closure's canonical JSON, so contracts minted at v2 are not
#: hash-comparable with v3 closures — the version says so.
CLOSURE_SCHEMA_VERSION = 3

#: Matches the Rust walk's bound. A cycle in `parent_group_id` should be
#: impossible, but "should be" is not a reason to write an unbounded loop
#: in something that runs on promotion.
MAX_HIERARCHY_DEPTH = 10

#: Governance/limit blobs on the resource group, in the order they appear
#: in the contract.
_LIMIT_FIELDS = (
    ("llm", "llm_global_limits"),
    ("mcp", "mcp_global_limits"),
    ("skill", "skill_global_limits"),
    ("media", "media_global_limits"),
)
_GOVERNANCE_FIELDS = (
    ("llm", "llm_global_governance"),
    ("mcp", "mcp_global_governance"),
    ("skill", "skill_global_governance"),
    ("media", "media_global_governance"),
)


# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------


def inheritance_chain(system: ResourceGroup) -> List[ResourceGroup]:
    """The groups whose resources this system can reach, nearest first.

    Always includes the system itself. Walks up while each group inherits,
    and stops at the first that does not — that group is included (its own
    bindings apply), its ancestors are not.
    """
    chain: List[ResourceGroup] = [system]
    current = system
    for _ in range(MAX_HIERARCHY_DEPTH):
        if not current.inherit_resources:
            break
        parent = current.parent_group
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return chain


# ---------------------------------------------------------------------------
# Restrictive merge
# ---------------------------------------------------------------------------


def merge_restrictive(blobs: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Min-wins merge of limit/governance blobs, nearest-first order.

    Numbers take the minimum, booleans take `True` (the restrictive value),
    nested dicts recurse. A key present in only one layer survives — a
    parent that sets a cap the child never mentions still caps the child.
    """
    merged: Dict[str, Any] = {}
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for key, value in blob.items():
            if key not in merged:
                merged[key] = value
                continue
            merged[key] = _strictest(merged[key], value)
    return merged


def _strictest(existing: Any, incoming: Any) -> Any:
    if isinstance(existing, dict) and isinstance(incoming, dict):
        return merge_restrictive([existing, incoming])
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    # bool before int: bool is a subclass of int in Python, and `min` over
    # two bools would silently answer the wrong question.
    if isinstance(existing, bool) or isinstance(incoming, bool):
        return bool(existing) or bool(incoming)
    if isinstance(existing, (int, float)) and isinstance(incoming, (int, float)):
        return min(existing, incoming)
    # Non-comparable (strings, lists): the nearer layer already won by
    # being first, so keep it rather than letting an ancestor override.
    return existing


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _binding_rows(db: Session, table, group_ids: List[str], id_column: str):
    return (
        db.query(table)
        .filter(table.c.resource_group_id.in_(group_ids))
        .order_by(table.c.resource_group_id, getattr(table.c, id_column))
        .all()
    )


def _llm_models(db: Session, chain: List[ResourceGroup]) -> List[Dict[str, Any]]:
    ids = [g.id for g in chain]
    rows = _binding_rows(db, resource_group_llm_models, ids, "llm_model_id")
    if not rows:
        return []
    models = {
        m.id: m
        for m in db.query(LLModel).filter(
            LLModel.id.in_({r.llm_model_id for r in rows})
        )
    }
    out = []
    for r in rows:
        model = models.get(r.llm_model_id)
        out.append(
            {
                "id": r.llm_model_id,
                "name": getattr(model, "name", None),
                "provider_id": getattr(model, "provider_id", None),
                "bound_via": r.resource_group_id,
                "inherited": r.resource_group_id != chain[0].id,
                "portal_visible": bool(r.portal_visible),
                "auto_bound": bool(getattr(r, "auto_bound", False)),
                "limits": r.limits or {},
            }
        )
    return _dedupe_nearest_first(out, chain)


def _mcp_servers(db: Session, chain: List[ResourceGroup]) -> List[Dict[str, Any]]:
    ids = [g.id for g in chain]
    rows = _binding_rows(db, resource_group_server_configs, ids, "server_config_id")
    if not rows:
        return []
    configs = {
        c.id: c
        for c in db.query(ServerConfig).filter(
            ServerConfig.id.in_({r.server_config_id for r in rows})
        )
    }
    out = []
    for r in rows:
        cfg = configs.get(r.server_config_id)
        out.append(
            {
                "id": r.server_config_id,
                "name": getattr(cfg, "name", None),
                "bound_via": r.resource_group_id,
                "inherited": r.resource_group_id != chain[0].id,
                "limits": r.limits or {},
            }
        )
    return _dedupe_nearest_first(out, chain)


def _skills(db: Session, chain: List[ResourceGroup]) -> List[Dict[str, Any]]:
    ids = [g.id for g in chain]
    rows = _binding_rows(db, resource_group_agent_skills, ids, "agent_skill_id")
    if not rows:
        return []
    skills = {
        s.id: s
        for s in db.query(AgentSkill).filter(
            AgentSkill.id.in_({r.agent_skill_id for r in rows})
        )
    }
    out = []
    for r in rows:
        skill = skills.get(r.agent_skill_id)
        out.append(
            {
                "id": r.agent_skill_id,
                "name": getattr(skill, "name", None),
                "bound_via": r.resource_group_id,
                "inherited": r.resource_group_id != chain[0].id,
                "limits": r.limits or {},
                "governance": r.governance or {},
                # The skill's own default: enforced by the core
                # (`determine_approval_required`), so an approver signs it.
                # Conditional, so a skill without one keeps its canonical JSON.
                **({"approval_required": True} if skill and skill.is_approval_required() else {}),
            }
        )
    return _dedupe_nearest_first(out, chain)


def _mcp_capability_gates(db: Session, system: ResourceGroup) -> List[Dict[str, Any]]:
    """MCP capabilities a person must approve, per server.

    Walked over self + ancestors with max strictness per capability, as the
    core resolves them: one group's `disabled` beats another's
    `approval_required`, and that capability is then refused, not held. The
    window is the smallest any group in the walk states.
    """
    groups = [system, *system.get_ancestors()]
    rows = (
        db.query(resource_group_mcp_capabilities)
        .filter(resource_group_mcp_capabilities.c.resource_group_id.in_([g.id for g in groups]))
        .all()
    )
    strictness = {"enabled": 0, "approval_required": 1, "disabled": 2}
    merged: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = (r.server_config_id, r.capability_type, r.capability_name)
        entry = merged.setdefault(key, {"access": "enabled", "window": None})
        if strictness.get(r.access, 0) > strictness[entry["access"]]:
            entry["access"] = r.access
        if r.approval_timeout_seconds and (
            entry["window"] is None or r.approval_timeout_seconds < entry["window"]
        ):
            entry["window"] = r.approval_timeout_seconds
    return [
        {
            "server_config_id": key[0],
            "capability_type": key[1],
            "capability_name": key[2],
            "approval_timeout_seconds": merged[key]["window"],
        }
        for key in sorted(merged)
        if merged[key]["access"] == "approval_required"
    ]


def _knowledge_bases(db: Session, chain: List[ResourceGroup]) -> List[Dict[str, Any]]:
    """KB collections the system can retrieve from.

    Bound to resource groups like every other resource and inherited the
    same way, but missing from the closure until now — so attaching a
    knowledge base (new content the system answers from) never drifted a
    contract. Same chain walk and nearest-first dedupe as the others.
    """
    ids = [g.id for g in chain]
    rows = _binding_rows(db, resource_group_kb_collections, ids, "kb_collection_id")
    if not rows:
        return []
    collections = {
        c.id: c
        for c in db.query(KbCollection).filter(
            KbCollection.id.in_({r.kb_collection_id for r in rows})
        )
    }
    out = []
    for r in rows:
        col = collections.get(r.kb_collection_id)
        out.append(
            {
                "id": r.kb_collection_id,
                "name": getattr(col, "name", None),
                "enabled": bool(getattr(col, "enabled", True)),
                "bound_via": r.resource_group_id,
                "inherited": r.resource_group_id != chain[0].id,
            }
        )
    return _dedupe_nearest_first(out, chain)


def _agent_identities(db: Session, system: ResourceGroup) -> List[Dict[str, Any]]:
    # Agents are members of *this* system, not inherited: an agent belongs
    # to the system it acts as, and inheriting them would attribute one
    # team's agents to another team's contract.
    rows = (
        db.query(ResourceGroupAgentMember)
        .filter(
            ResourceGroupAgentMember.tenant_id == system.tenant_id,
            ResourceGroupAgentMember.group_id == system.id,
        )
        .order_by(ResourceGroupAgentMember.agent_id)
        .all()
    )
    if not rows:
        return []
    agents = {
        a.id: a
        for a in db.query(AgentIdentity).filter(
            AgentIdentity.id.in_({r.agent_id for r in rows})
        )
    }
    return [_agent_entry(r, agents.get(r.agent_id)) for r in rows]


def _agent_entry(member: ResourceGroupAgentMember, agent: Optional[AgentIdentity]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "id": member.agent_id,
        "name": getattr(agent, "name", None),
        "role": member.role,
        "enforcement_mode": getattr(agent, "enforcement_mode", None),
        "autonomy_level": getattr(agent, "autonomy_level", None),
    }
    # The implementation declaration is part of what an
    # approver signs: which software this identity must be, which of its
    # releases are accepted, and whether a run without a release is a
    # finding. Conditional keys (the envelope's reason): an identity that
    # declares nothing keeps the system's canonical JSON; declaring drifts
    # the contract once, and changing the approved versions drifts it again.
    if agent is not None:
        if agent.implementation_name:
            entry["implementation_name"] = agent.implementation_name
        approved = sorted(agent.approved_versions or [])
        if approved:
            entry["approved_versions"] = approved
        if agent.require_release:
            entry["require_release"] = True
    return entry


def _agent_grants(db: Session, system: ResourceGroup) -> List[Dict[str, Any]]:
    """What the agents in this system are actually allowed to *do*.

    The identity list above answers "who acts as this system"; this answers
    "and what may they do", which is the question the contract exists for.
    Without it a contract could claim to be the resolved closure of
    everything governing a system while the allow/deny/approval decisions
    on every one of its actions sat outside the hash — an operator could
    widen an agent from `approval_required` to `allow` and nothing would
    drift, mint, or need re-approving.

    Two deliberate choices:

    * **Only `enabled` grants**, because disabling one is an operator act
      that genuinely narrows the agent and should mint a new version.
    * **Expired grants are still listed, carrying their `expires_at`**
      rather than filtered out. Filtering on `now()` would make the hash a
      function of the clock: a grant lapsing overnight would flip a system
      to config-drifted with nobody having touched anything, and no action
      an operator could take to explain it. The expiry is a *term of the
      contract*, so it belongs in the contract text.

    Sorted, so two identical configurations always hash identically.
    """
    return agent_grants_for_systems(db, system.tenant_id, [system.id]).get(system.id, [])


def agent_grants_for_systems(
    db: Session, tenant_id: str, system_ids: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """`{system id: its closure agent_grants}` for many systems in two queries.

    The ONE serialization of "what this system's agents may do": the closure
    (one system) and the composition checks (every system in a tenant, the
    grant/edge checks) both read it, so a grant the contract
    covers is exactly a grant the checks see.
    """
    if not system_ids:
        return {}
    members = (
        db.query(ResourceGroupAgentMember.group_id, ResourceGroupAgentMember.agent_id)
        .filter(
            ResourceGroupAgentMember.tenant_id == tenant_id,
            ResourceGroupAgentMember.group_id.in_(system_ids),
        )
        .all()
    )
    if not members:
        return {}
    systems_of: Dict[str, List[str]] = {}
    for group_id, agent_id in members:
        systems_of.setdefault(agent_id, []).append(group_id)
    names = {
        a.id: a.name
        for a in db.query(AgentIdentity).filter(AgentIdentity.id.in_(list(systems_of)))
    }
    grants = (
        db.query(AgentGrant)
        .filter(
            AgentGrant.tenant_id == tenant_id,
            AgentGrant.agent_id.in_(list(systems_of)),
            AgentGrant.enabled.is_(True),
        )
        .all()
    )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for g in grants:
        entry = {
            "agent_id": g.agent_id,
            "agent_name": names.get(g.agent_id),
            "action_type": g.action_type,
            "target": g.target,
            "effect": g.effect,
            "constraints": g.constraints or None,
            "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        }
        for sid in systems_of.get(g.agent_id, []):
            out.setdefault(sid, []).append(dict(entry))
    for entries in out.values():
        entries.sort(
            key=lambda g: (
                g["agent_id"] or "",
                g["action_type"] or "",
                g["target"] or "",
                g["effect"] or "",
            )
        )
    return out


def a2a_call_targets(grants: List[Dict[str, Any]]) -> List[str]:
    """Agent cards the `a2a_call` grants reach: enabled (already filtered),
    not deny, and naming a card (a `*` wildcard names none). Sorted."""
    return sorted(
        {
            g["target"]
            for g in grants or []
            if g.get("action_type") == "a2a_call"
            and g.get("effect") != "deny"
            and g.get("target")
            and g.get("target") != "*"
        }
    )


def _dedupe_nearest_first(
    entries: List[Dict[str, Any]], chain: List[ResourceGroup]
) -> List[Dict[str, Any]]:
    """One entry per resource, keeping the nearest binding.

    A resource bound at both the system and an ancestor is one grant, not
    two, and the nearer binding carries the limits that actually apply.
    Sorted by id at the end so the hash does not depend on row order.
    """
    rank = {g.id: i for i, g in enumerate(chain)}
    best: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        key = entry["id"]
        if key not in best or rank.get(entry["bound_via"], 99) < rank.get(
            best[key]["bound_via"], 99
        ):
            best[key] = entry
    return sorted(best.values(), key=lambda e: e["id"])


def _related_ids(relationship, attr: str = "id") -> List[str]:
    """Ids from a `lazy='dynamic'` relationship, sorted."""
    try:
        return sorted(str(getattr(o, attr)) for o in relationship)
    except Exception:  # pragma: no cover - relationship not configured in tests
        return []


# ---------------------------------------------------------------------------
# The closure
# ---------------------------------------------------------------------------


# Volatile bookkeeping that must not move a contract hash: WHO touched a row
# and WHEN says nothing about what the control does.
_AUDIT_KEYS = frozenset(
    {"created_at", "updated_at", "created_by", "updated_by", "last_used_at"}
)


def _content_fingerprint(obj: Any) -> Optional[str]:
    """A short, stable hash of a governance object's semantic content.

    This is the difference between a contract that names its guardrails and
    one that *pins* them. An id list stays identical while an operator turns
    PII redaction off inside the config — the most safety-relevant class of
    change there is, previously invisible to `config_drifted` and to every
    diff an approver read. Hashing the content (minus audit bookkeeping)
    makes any semantic edit change the closure, and therefore the hash,
    and therefore the gate's answer.
    """
    try:
        d = obj.to_dict()
    except Exception:
        return None
    clean = {k: v for k, v in d.items() if k not in _AUDIT_KEYS}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _governance_objects(system: ResourceGroup, attr: str) -> List[Dict[str, Any]]:
    """Chain-walked, content-fingerprinted snapshot of bound governance
    objects (guardrails / argument policies / cache configs).

    Walked over self + ancestors because governance flows down uncondition-
    ally — a guardrail attached to the parent org governs this system at
    runtime, and a contract that omits it would certify less than what is
    actually enforced.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for group in [system, *system.get_ancestors()]:
        for obj in getattr(group, attr, []) or []:
            oid = str(obj.id)
            if oid in seen:
                continue
            seen[oid] = {
                "id": oid,
                "name": getattr(obj, "name", None),
                "enabled": bool(getattr(obj, "enabled", True)),
                "bound_via": group.id,
                "inherited": group.id != system.id,
                "content_sha256": _content_fingerprint(obj),
            }
    return sorted(seen.values(), key=lambda o: o["id"])


def _response_policies(db: Session, system: ResourceGroup) -> List[Dict[str, Any]]:
    """Response policies in scope — they are part of the contract.

    Arming an automatic actuator is among the most consequential controls a
    system carries; a contract that does not cover it certifies a system
    while staying silent about what may restrict it unattended.
    """
    from app.services.ai_system_response.actuator import policies_in_scope

    return [
        {
            "id": pol.id,
            "name": pol.name,
            "enabled": bool(pol.enabled),
            "attached_to": pol.ai_system_id,
            "inherited": pol.ai_system_id != system.id,
            "signal_kind": pol.signal_kind,
            "drift_class": pol.drift_class,
            "severity_at_least": pol.severity_at_least,
            "actions": pol.actions or [],
            "allow_suspend": bool(pol.allow_suspend),
            "require_human_to_restore": bool(pol.require_human_to_restore),
            "response_cooldown_seconds": pol.response_cooldown_seconds,
        }
        for pol in sorted(
            policies_in_scope(db, system, enabled_only=False), key=lambda p: p.id
        )
    ]


def dependency_contract(
    db: Session, tenant_id: str, target_system_id: Optional[str], pin: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The contract a shared-service edge runs against: `{id, version, hash}`.

    The target's ACTIVE contract when the edge follows it (`pin` NULL), else
    the pinned one (any status — a pin names a version, and a superseded
    version is still the one the caller approved against). `None` when the
    target has no such contract. The closure and the Composition view both
    read this, so what an approver signs is what the tab shows.
    """
    from app.database.models.ai_system_contract import AISystemContract, CONTRACT_ACTIVE

    if not target_system_id:
        return None
    q = db.query(AISystemContract).filter(
        AISystemContract.tenant_id == tenant_id,
        AISystemContract.ai_system_id == target_system_id,
    )
    row = (
        q.filter(AISystemContract.id == pin).first()
        if pin
        else q.filter(AISystemContract.status == CONTRACT_ACTIVE).first()
    )
    if row is None:
        return None
    return {"id": row.id, "version": row.version, "hash": row.contract_hash}


def _dependencies(db: Session, system: ResourceGroup) -> List[Dict[str, Any]]:
    """The declared dependency edges, as contract terms.

    For a shared service the entry carries the target's contract `{id,
    version, hash}` — the active one when the edge follows it, the pinned
    one otherwise. That is the whole mechanism of dependency drift: a
    service promoting a new contract changes every FOLLOWING caller's
    closure hash, so the caller is config-drifted and its approver sees the
    service's change in the diff. A pinned caller's hash does not move; the
    composition check `dependency_pin_stale` says the pin is behind instead.

    No row ids (they change on reseed) and no names (cosmetic; a rename of
    the service reaches the caller through the service's own contract).
    Sorted, so identical edge sets hash identically.
    """
    from app.database.models.ai_system_dependency import (
        ROLE_SHARED_SERVICE,
        AISystemDependency,
    )

    edges = (
        db.query(AISystemDependency)
        .filter(
            AISystemDependency.tenant_id == system.tenant_id,
            AISystemDependency.ai_system_id == system.id,
        )
        .all()
    )
    out: List[Dict[str, Any]] = []
    for e in edges:
        entry: Dict[str, Any] = {
            "role": e.role,
            "depends_on_system_id": e.depends_on_system_id,
            "agent_card_id": e.agent_card_id,
            "criticality": e.criticality,
            "contributes_to_decision": bool(e.contributes_to_decision),
            "fallback": e.fallback,
            "separability_rationale": e.separability_rationale,
            "assumptions": e.assumptions or {},
        }
        if e.role == ROLE_SHARED_SERVICE:
            entry["contract_pin"] = e.contract_pin
            entry["contract"] = dependency_contract(
                db, system.tenant_id, e.depends_on_system_id, e.contract_pin
            )
        # Who supplies the dependency is a term the
        # approver signs (it decides the Art 25(4) supplier obligation).
        # Conditional: an edge without one keeps its canonical JSON. The
        # separability record id is NOT a term — it evidences the rationale,
        # which already is one.
        if e.supplier_legal_entity:
            entry["supplier_legal_entity"] = e.supplier_legal_entity
        # The delegate outcomes the caller is built to handle
        # are a term the approver signs. Conditional (sorted): an edge
        # without them keeps its canonical JSON.
        if e.expected_outcomes is not None:
            entry["expected_outcomes"] = sorted(e.expected_outcomes)
        out.append(entry)
    out.sort(
        key=lambda d: (
            d["role"] or "",
            d["depends_on_system_id"] or "",
            d["agent_card_id"] or "",
        )
    )
    return out


def resolve_closure(db: Session, system: ResourceGroup) -> Dict[str, Any]:
    """Everything that governs this AI System, as hashable JSON."""
    from app.services.ai_system_envelope import validate_envelope

    chain = inheritance_chain(system)

    # Included only when declared: adding the key unconditionally would
    # change the canonical JSON of every existing system and mark the whole
    # fleet config-drifted the day this ships. A system that *sets* an
    # envelope drifts from its active contract — which is exactly right,
    # because the new terms need approving.
    envelope = validate_envelope(system.operating_envelope)

    closure: Dict[str, Any] = {
        "schema_version": CLOSURE_SCHEMA_VERSION,
        "ai_system": {
            "id": system.id,
            "name": system.name,
            "system_kind": system.system_kind,
            # Deliberately NOT lifecycle_stage — see CLOSURE_SCHEMA_VERSION.
            "owner": system.owner,
            "intended_use": system.intended_use,
            "intended_client_labels": system.intended_client_labels,
            # Sorted: a set of callers, and the hash must not depend on the
            # order an operator ticked them in.
            "intended_client_systems": (
                sorted(system.intended_client_systems)
                if system.intended_client_systems is not None
                else None
            ),
            "eu_ai_act_risk_tier": system.eu_ai_act_risk_tier,
            "phi_resource": bool(system.phi_resource),
            "baseline_opt_out": bool(system.baseline_opt_out),
            "run_idle_timeout_seconds": system.run_idle_timeout_seconds,
        },
        # The identity policy the contract was minted under: the system's
        # STORED policy (normalised at write), null when unset. Not the
        # effective policy: that tightens with the lifecycle stage, so
        # promoting a contract would read as drift (the reason the stage
        # itself is not here either).
        "identity_policy": copy.deepcopy(system.identity_policy),
        # Nearest first, so a reader can see where an inherited control
        # came from without re-deriving the hierarchy.
        "inheritance_chain": [g.id for g in chain],
        "inherits_from_parent": bool(system.inherit_resources),
        "autonomy_level": resolve_autonomy(*[g.autonomy_level for g in chain]),
        "composition": {
            "llm_models": _llm_models(db, chain),
            "mcp_servers": _mcp_servers(db, chain),
            "skills": _skills(db, chain),
            "agent_identities": _agent_identities(db, system),
            "guardrails": _governance_objects(system, "guardrail_configs"),
            "argument_policies": _governance_objects(system, "argument_policies"),
            "cache_configs": _governance_objects(system, "cache_configs"),
        },
        "response_policies": _response_policies(db, system),
        "limits": {
            name: merge_restrictive([getattr(g, field, None) for g in chain])
            for name, field in _LIMIT_FIELDS
        },
        "governance": {
            name: merge_restrictive([getattr(g, field, None) for g in chain])
            for name, field in _GOVERNANCE_FIELDS
        },
    }
    if envelope:
        closure["operating_envelope"] = envelope
    # The outcome catalogue: how a run may end and which ends
    # count toward the baseline. Normalised (sorted by id, variants sorted)
    # so identical catalogues hash identically. Conditional for the
    # envelope's reason; migration `a203_declared_outcomes` seeds every existing AI System with the
    # starter catalogue, so each drifts once and its next approved contract
    # carries the catalogue the core judges outcomes against.
    from app.services.ai_system_outcome_catalogue import validate_catalogue

    outcomes = validate_catalogue(system.declared_outcomes)
    if outcomes:
        closure["outcomes"] = outcomes
    # The EU AI Act role (provider / deployer / …) is part of what an
    # approver signs, next to the risk tier. Conditional for the same
    # reason as the envelope: an unconditional key would re-hash every
    # existing contract; a system that declares a role drifts once, and
    # the next version carries it.
    if system.eu_ai_act_role:
        closure["ai_system"]["eu_ai_act_role"] = system.eu_ai_act_role
    # Whether the service REFUSES callers outside
    # intended_client_systems is part of what its approver signs.
    # Conditional (the envelope's reason): a service that does not enforce
    # keeps its canonical JSON; turning enforcement on drifts it once.
    if system.enforce_accepted_callers:
        closure["ai_system"]["enforce_accepted_callers"] = True
    # How long a human has to decide a hold this system raises (the
    # group chain's min-wins window; the tenant / platform fallback is not
    # the system's declaration). Conditional (the envelope's reason): a
    # system whose chain states no window keeps its canonical JSON; stating
    # or changing one drifts the contract once — an approver signs how long
    # oversight may take.
    from app.services.approval_window import chain_min

    approval_window = chain_min(system)
    if approval_window is not None:
        closure["approval_window_seconds"] = approval_window

    # Same conditional treatment as the envelope above, for the same reason:
    # adding this key unconditionally would rewrite the canonical JSON of
    # every existing contract and mark the entire fleet config-drifted on
    # the day it ships. A system whose agents actually *hold* grants does
    # drift once — correctly, because terms that were never covered are now
    # part of what an approver signs.
    agent_grants = _agent_grants(db, system)
    if agent_grants:
        closure["agent_grants"] = agent_grants

    # Which MCP capabilities a person must approve sat outside the
    # hash: relaxing one to `enabled` removed a human from the loop and nothing
    # drifted. Conditional like `agent_grants`: a system with none keeps its
    # canonical JSON; one with gates drifts once and the next version covers
    # them.
    capability_gates = _mcp_capability_gates(db, system)
    if capability_gates:
        closure["mcp_capability_gates"] = capability_gates

    # Knowledge bases, conditional for the same reason as
    # `agent_grants`: a system with no KB keeps its canonical JSON; one that
    # retrieves from a KB drifts once and the next version covers it.
    knowledge_bases = _knowledge_bases(db, chain)
    if knowledge_bases:
        closure["composition"]["knowledge_bases"] = knowledge_bases

    # Declared dependency edges. Conditional like
    # `agent_grants`: a system that declares none keeps its canonical JSON;
    # one that does drifts once and the next version covers its edges.
    dependencies = _dependencies(db, system)
    if dependencies:
        closure["dependencies"] = dependencies

    # Which records a run may touch and how many one
    # requester may reach. Conditional like `agent_grants`: a system without a
    # data scope keeps its canonical JSON; setting one drifts it once.
    data_scope = _data_scope(db, system)
    if data_scope:
        closure["data_scope"] = data_scope
    return closure


def _data_scope(db: Session, system: ResourceGroup) -> Optional[Dict[str, Any]]:
    from app.database.models.ai_system_data_scope import (
        AISystemDataScope,
        AISystemReachRule,
    )

    row = (
        db.query(AISystemDataScope)
        .filter(
            AISystemDataScope.tenant_id == system.tenant_id,
            AISystemDataScope.ai_system_id == system.id,
        )
        .first()
    )
    rules = (
        db.query(AISystemReachRule)
        .filter(
            AISystemReachRule.tenant_id == system.tenant_id,
            AISystemReachRule.ai_system_id == system.id,
        )
        .order_by(AISystemReachRule.id)
        .all()
    )
    if row is None and not rules:
        return None
    out: Dict[str, Any] = {}
    if row is not None:
        out.update(
            {
                "mode": row.mode,
                "on_unbound": row.on_unbound,
                "keys": sorted(row.keys or [], key=lambda k: k.get("key", "")),
                "rules": sorted(
                    row.rules or [],
                    key=lambda r: (r.get("tool", ""), r.get("argument", ""), r.get("key", "")),
                ),
                "unscoped_tools": sorted(row.unscoped_tools or []),
            }
        )
    if rules:
        out["reach_rules"] = [
            {
                "id": r.id,
                "scope_key": r.scope_key,
                "group_by": r.group_by,
                "window_hours": r.window_hours,
                "max_distinct": r.max_distinct,
                "severity": r.severity,
                "enabled": bool(r.enabled),
            }
            for r in rules
        ]
    return out


def resolve_and_hash(db: Session, system: ResourceGroup) -> tuple[Dict[str, Any], str]:
    closure = resolve_closure(db, system)
    return closure, hash_closure(closure)


__all__ = [
    "CLOSURE_SCHEMA_VERSION",
    "DEFAULT_AUTONOMY_LEVEL",
    "MAX_HIERARCHY_DEPTH",
    "a2a_call_targets",
    "agent_grants_for_systems",
    "dependency_contract",
    "inheritance_chain",
    "merge_restrictive",
    "resolve_and_hash",
    "resolve_closure",
]
