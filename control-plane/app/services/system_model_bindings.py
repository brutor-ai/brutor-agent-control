# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Keep "system model" resource-group bindings in sync.

Some features call an LLM *on the caller's behalf* rather than because the
caller asked for it:

  * **Semantic policy** — the judge model that rules on the request.
  * **Semantic cache** — the embedding model that builds the cache key.
  * **Knowledge bases** — the embedding model that embeds the user's
    question for retrieval. Without it bound, grounding 403'd on the
    query embedding and the chat went on ungrounded, logged only as a
    warning: a workspace with a knowledge base silently had none.

Those calls go through the gateway's own governed endpoints, so they are
subject to the same resource-group access check as any other request.
That is deliberate: it is what makes cost attribution, usage counters,
proxy logs and quotas work for them with no special cases. The failure
mode it created was that an admin could configure a semantic policy with
a judge model that the calling group could not access, and the policy
would then silently fail open at runtime.

Rather than exempt these models from access control, we make the binding
follow the configuration: whenever a feature is bound to a resource
group, the model it needs is bound to that group too.

Rules, in order of precedence:

  1. **Never touch an existing binding.** If the model is already bound to
     the group — by an admin, or by another feature — it is left exactly
     as-is, `portal_visible` included. An admin who deliberately exposed
     the model keeps it exposed.
  2. **New bindings are hidden.** A binding we create gets
     `portal_visible = False`: the group needs *access* to the model, but
     it should not clutter the end-user model picker.
  3. **Only reclaim what we created.** On teardown we delete rows with
     `auto_bound = True` only, and only when no remaining feature needs
     that (group, model) pair.

`sync_tenant` recomputes the whole tenant rather than diffing one config.
That makes it idempotent and immune to refcount bugs when two policies
share a judge model, and it means every caller — create, update, delete,
access-control change — runs the identical code path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.kb_collection import KbCollection, resource_group_kb_collections
from app.database.models.resource_group import resource_group_llm_models
from app.database.models.semantic_cache_config import (
    SemanticCacheConfig,
    resource_group_cache_configs,
)
from app.database.models.semantic_policy import (
    SemanticPolicy,
    resource_group_semantic_policies,
)
from app.database.models.tenant import Tenant

# (resource_group_id, llm_model_id)
Binding = Tuple[str, str]


def _tenant_defaults(db: Session, tenant_id: str) -> Tuple[Optional[str], Optional[str]]:
    """(default_judge_model_id, default_embedding_model_id) for the tenant."""
    row = (
        db.query(Tenant.default_judge_model_id, Tenant.default_embedding_model_id)
        .filter(Tenant.id == tenant_id)
        .first()
    )
    return (row[0], row[1]) if row else (None, None)


def compute_required_bindings(db: Session, tenant_id: str) -> Set[Binding]:
    """Every (group, model) pair a system feature needs in this tenant.

    A feature with no resolvable model contributes nothing — we cannot
    bind a model we cannot name. (The runtime's last-resort "first model
    alphabetically" judge fallback is deliberately NOT modelled here: it
    is not an operator decision, so auto-binding to it would silently
    grant access nobody asked for.)
    """
    default_judge, default_embedding = _tenant_defaults(db, tenant_id)
    required: Set[Binding] = set()

    # ── Semantic policies → judge model ────────────────────────────────
    policies = (
        db.query(SemanticPolicy.id, SemanticPolicy.judge_model_id)
        .filter(SemanticPolicy.tenant_id == tenant_id)
        .all()
    )
    for policy_id, judge_model_id in policies:
        model_id = judge_model_id or default_judge
        if not model_id:
            continue
        group_ids = db.execute(
            select(resource_group_semantic_policies.c.resource_group_id).where(
                resource_group_semantic_policies.c.semantic_policy_id == policy_id
            )
        ).scalars().all()
        required.update((gid, model_id) for gid in group_ids)

    # ── Semantic cache configs → tenant embedding model ────────────────
    # The cache has no per-config model: every entry must live in one
    # vector space per tenant, so it always uses the tenant default.
    if default_embedding:
        configs = (
            db.query(SemanticCacheConfig.id)
            .filter(SemanticCacheConfig.tenant_id == tenant_id)
            .all()
        )
        for (config_id,) in configs:
            group_ids = db.execute(
                select(resource_group_cache_configs.c.resource_group_id).where(
                    resource_group_cache_configs.c.cache_config_id == config_id
                )
            ).scalars().all()
            required.update((gid, default_embedding) for gid in group_ids)

    # ── Knowledge-base collections → tenant embedding model ────────────
    # Retrieval embeds the question in the tenant's single KB vector
    # space, so it too always uses the tenant default.
    if default_embedding:
        group_ids = db.execute(
            select(resource_group_kb_collections.c.resource_group_id)
            .join(
                KbCollection,
                KbCollection.id == resource_group_kb_collections.c.kb_collection_id,
            )
            .where(KbCollection.tenant_id == tenant_id)
        ).scalars().all()
        required.update((gid, default_embedding) for gid in group_ids)

    return required


def sync_tenant(db: Session, tenant_id: str, actor: str = "system") -> Dict[str, int]:
    """Reconcile auto-bindings for one tenant. Caller commits.

    Returns counts for logging/tests: ``{"added": n, "removed": n, "kept": n}``.
    """
    required = compute_required_bindings(db, tenant_id)

    # Current bindings for the groups we care about. Restrict to this
    # tenant's groups by joining through the pairs we know about plus any
    # existing auto-bound rows (a group may have dropped out of `required`
    # entirely, and we still need to reclaim its row).
    existing_rows = db.execute(
        select(
            resource_group_llm_models.c.resource_group_id,
            resource_group_llm_models.c.llm_model_id,
            resource_group_llm_models.c.auto_bound,
        )
    ).all()
    existing: Dict[Binding, bool] = {
        (rg, model): bool(auto) for rg, model, auto in existing_rows
    }

    added = removed = kept = 0
    now = datetime.now(timezone.utc)

    # ── Add what's missing ─────────────────────────────────────────────
    for rg_id, model_id in sorted(required):
        if (rg_id, model_id) in existing:
            # Rule 1: never touch an existing binding (manual or auto).
            kept += 1
            continue
        db.execute(
            resource_group_llm_models.insert().values(
                id=str(uuid.uuid4()),
                resource_group_id=rg_id,
                llm_model_id=model_id,
                created_at=now,
                created_by=actor,
                # Rule 2: access yes, portal clutter no.
                portal_visible=False,
                auto_bound=True,
            )
        )
        added += 1

    # ── Reclaim auto-bindings nothing needs any more ───────────────────
    # Rule 3: `auto_bound` is the licence to delete. Manual bindings for
    # the same pair are invisible to this loop.
    for (rg_id, model_id), is_auto in existing.items():
        if not is_auto or (rg_id, model_id) in required:
            continue
        # Only reclaim rows belonging to THIS tenant's groups. A pair that
        # is not required by this tenant may still be required by another,
        # so scope the delete by the tenant's own policy/cache groups.
        if not _group_belongs_to_tenant(db, rg_id, tenant_id):
            continue
        db.execute(
            resource_group_llm_models.delete().where(
                resource_group_llm_models.c.resource_group_id == rg_id,
                resource_group_llm_models.c.llm_model_id == model_id,
                resource_group_llm_models.c.auto_bound.is_(True),
            )
        )
        removed += 1

    return {"added": added, "removed": removed, "kept": kept}


def _group_belongs_to_tenant(db: Session, group_id: str, tenant_id: str) -> bool:
    from app.database.models.resource_group import ResourceGroup

    return (
        db.query(ResourceGroup.id)
        .filter(ResourceGroup.id == group_id, ResourceGroup.tenant_id == tenant_id)
        .first()
        is not None
    )
