# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The models the gateway forwards to: routing-group members and fallbacks.

A request names one model; the gateway may send it to another: a member of
the routing group the named model stands for, or a fallback of the model or
of the group. Each candidate passes the gateway's dispatch check where it is
about to be sent (`providers::dispatch_refusal` with
`ProviderApi::ChatForward` in `llm_router::forward_chat_completion`, and in
`resolve_fallback_chain` with the API of the route), and one it cannot send
is skipped with a log line. A cloud row is served there only by Converse
(chat) and by the Bedrock embedding backends; a Claude or Gemini row on
Bedrock or Vertex AI, which the gateway does serve when a request names it,
is dropped as a member or fallback on every request.

The control plane refuses such a row as a member or a fallback when it is
saved. Which rows: `app/services/llm/destination.py` (`forward_problem`,
read off its copy of the gateway's `cloud_backend`). This module finds the
rows an entry of a fallback list names, as the gateway resolves it
(`llm_router::try_fallback_list`: a routing group by id or name first, then
a model by `model_name` or id).

A candidate the gateway does not serve at all is refused in the same
places: a retired model, and a model of a disabled provider
(`app/services/llm/availability.py`, the gateway's one availability rule).
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.constants.llm import LLModelMode
from app.database.models.llm_model import LLModel
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.services.llm import availability
from app.services.llm.destination import forward_problem

MEMBER = "a member of a routing group"
FALLBACK = "a fallback"
# The lists of a routing group the gateway walks with the chat forwarder.
GROUP_FALLBACK_FIELDS = (
    "fallback_models",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "default_fallbacks",
)


def entry_names(entries: Any) -> List[str]:
    """The names in a fallback list, as `try_fallback_list` reads it: a
    string, or the strings in the lists an object maps to."""
    names: List[str] = []
    for item in entries if isinstance(entries, list) else []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, list):
                    names.extend(v for v in value if isinstance(v, str))
    return names


def member_problem(
    provider: Optional[str],
    model_name: Optional[str],
    mode: Optional[str],
    not_served: Optional[str] = None,
) -> Optional[str]:
    """Why a row cannot be a member of a routing group of its mode.

    `not_served` is why the gateway does not serve the row at all
    (`availability.not_served_reason`), when it does not."""
    if mode == LLModelMode.ROUTING.value:
        return None
    if not_served:
        return f"'{model_name}' cannot join a routing group: {not_served}, and the gateway does not serve it"
    return forward_problem(provider, model_name, mode, MEMBER)


def fallback_problem(
    db: Session, tenant_id: str, entries: Any, mode: Optional[str], field: str
) -> Optional[str]:
    """Why a fallback list of a model or group in `mode` cannot be saved:
    the first entry that names a row the gateway would drop. An entry that
    names a routing group is that group (its members are checked when they
    join it); one that names nothing is skipped by the gateway and here."""
    for name in entry_names(entries):
        group = (
            db.query(LLMRoutingGroup.id)
            .filter(LLMRoutingGroup.tenant_id == tenant_id, or_(LLMRoutingGroup.id == name, LLMRoutingGroup.name == name))
            .first()
        )
        if group is not None:
            continue
        rows: Iterable[LLModel] = (
            db.query(LLModel)
            .filter(LLModel.tenant_id == tenant_id, or_(LLModel.model_name == name, LLModel.id == name))
            .all()
        )
        for row in rows:
            problem = forward_problem(row.provider.key if row.provider else None, row.model_name, mode, FALLBACK)
            if problem:
                return f"{field}: {problem}"
            not_served = availability.row_not_served_reason(row)
            if not_served:
                return (
                    f"{field}: '{name}' ({row.model_name}) cannot be a fallback: {not_served}, "
                    "and the gateway does not serve it"
                )
    return None


def group_fallbacks_problem(db: Session, tenant_id: str, group_data: Any) -> Optional[str]:
    """Why the fallback lists in a routing group's data cannot be saved.
    Every list of a group is walked by the chat forwarder."""
    for field in GROUP_FALLBACK_FIELDS:
        problem = fallback_problem(db, tenant_id, group_data.get(field), LLModelMode.CHAT.value, field)
        if problem:
            return problem
    return None
