# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Shared AI Resource API fields — version + authorship

Mirrors `app/database/models/resource_mixins.py` on the wire.

Two mixins, kept separate because they behave differently:

  * [`ResourceVersionField`] is **writable**. The operator owns the label.
  * [`ResourceAuthorshipFields`] is **read-only**. It is stamped by the
    system on flush and is deliberately absent from every request model —
    a field recording *who did this* that a client can set records nothing.

The read-only guarantee is enforced by omission rather than by validation:
because `created_by` / `updated_by` never appear on a Create or Update
model, Pydantic drops them from any request body that tries. There is no
code path that could be persuaded to honour them, which is a stronger
guarantee than a validator that someone could later relax.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.database.models.resource_mixins import DEFAULT_RESOURCE_VERSION


class ResourceVersionField(BaseModel):
    """Operator-authored version label. Writable."""

    version: Optional[str] = Field(
        None,
        max_length=50,
        description=(
            "Operator-authored version label (defaults to "
            f"'{DEFAULT_RESOURCE_VERSION}'). A handle for humans — change "
            "history lives in the audit log, not here."
        ),
    )

    @model_validator(mode="after")
    def _a_blank_version_means_unset(self):
        """Treat a null or blank `version` as "not supplied".

        The column is NOT NULL with a default, so neither is ever a
        meaningful instruction — there is no state the resource could move
        to. But update endpoints apply their patch with a generic
        `setattr` loop over `model_dump(exclude_unset=True)`, so a client
        that helpfully serialises every field lands an explicit NULL in a
        NOT NULL column and the whole update fails with an IntegrityError.

        Clearing the field from `model_fields_set` makes `exclude_unset`
        drop it, which is what the operator meant: leave the version alone.
        Doing it here rather than in each endpoint keeps the nine resources
        that share this mixin from having to remember individually.
        """
        if self.version is not None and self.version.strip():
            return self
        self.version = None
        self.__pydantic_fields_set__.discard("version")
        return self


class ResourceAuthorshipFields(BaseModel):
    """Who created and last modified this resource. Read-only.

    Response-side only. Never inherited by a Create or Update model.
    """

    created_by: Optional[str] = Field(
        None, description="Admin who created this resource. System-set; read-only."
    )
    updated_by: Optional[str] = Field(
        None,
        description="Admin who last modified this resource. System-set; read-only.",
    )
    # The timestamps belong to the same block. They are declared per-model
    # as *columns* — they predate authorship tracking and carry different
    # server defaults — but the Audit Info tab renders "who" and "when"
    # together, so a response that carries one without the other is half an
    # answer. Optional here so a model that already declares them
    # (required, non-null) keeps its own stricter version.
    created_at: Optional[str] = Field(
        None, description="When this resource was created (ISO 8601). Read-only."
    )
    updated_at: Optional[str] = Field(
        None, description="When this resource was last modified (ISO 8601). Read-only."
    )


class ResourceMetaFields(ResourceAuthorshipFields):
    """Authorship plus the version label, for response models.

    The General tab renders exactly this block alongside id, name and
    description.
    """

    version: Optional[str] = Field(
        None, description="Operator-authored version label."
    )


__all__ = [
    "ResourceVersionField",
    "ResourceAuthorshipFields",
    "ResourceMetaFields",
]
