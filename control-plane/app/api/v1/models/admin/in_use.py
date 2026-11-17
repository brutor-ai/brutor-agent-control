# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The body of a refusal to delete something that is in use.

Every such refusal of the admin API answers 409 with this body: a sentence
for a person, and `used_by` for a program, so that no client reads the
sentence to find out what to show.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from fastapi import HTTPException
from pydantic import BaseModel, Field


class UsedBy(BaseModel):
    """A row that depends on the one that was to be deleted."""

    id: str
    name: str


class InUse(BaseModel):
    message: str = Field(..., description="The refusal as a sentence, naming what uses the row.")
    used_by: Dict[str, List[UsedBy]] = Field(
        ...,
        description=(
            "What depends on the row, by kind (the key says which: `virtual_servers`, "
            "`server_configs`, `administrators`, ...). A kind nothing of which uses it is left out."
        ),
    )


class InUseRefusal(BaseModel):
    """The body of a 409 that refuses to delete a row in use."""

    detail: InUse


def in_use(message: str, **used_by: Iterable[Any]) -> HTTPException:
    """The 409 for a row in use. Each keyword is a kind and the rows of
    that kind that use it: objects with `id` and a name (`name`, else
    `username`)."""
    listed = {
        kind: [
            {"id": str(row.id), "name": str(getattr(row, "name", None) or getattr(row, "username", None) or row.id)}
            for row in rows
        ]
        for kind, rows in used_by.items()
    }
    return HTTPException(
        status_code=409,
        detail={"message": message, "used_by": {kind: rows for kind, rows in listed.items() if rows}},
    )


IN_USE_RESPONSE = {"model": InUseRefusal}
