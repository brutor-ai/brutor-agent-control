# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI-interaction notice configuration

What the portal shows a person before the first substantive turn, per
tenant (the default, `ai_system_id` NULL) and per AI System (an override).
A framework-neutral capability — EU AI Act Art 50(1), Colorado, Korea and
ISO A.8.2 all cite it.

* `texts` = `{locale: text}`; `text_digests` = `{locale: hex SHA-256 of
  the UTF-8 text}`. The digest is what the portal's rendered-notice record
  references, so the text itself never enters a record. A plain hash, not a
  signature: the CP computes it with hashlib and never signs.
* An `exemption` (`{asserted, basis, rationale}` — e.g. Art 50(1) "obvious
  from the circumstances") is a declaration: it is sealed as its own
  `ai.brutor.declaration` record and shown as DECLARED, never as evidence
  that the notice was unnecessary.
* Every save (and a delete) seals an `ai.brutor.notice-config` record whose
  payload digest covers the full configuration and whose references carry
  each locale's text digest.

Resolution order the core applies for the portal: system row → tenant row
→ the default `{enabled: true, surface: "both"}` with no text (the portal
uses its i18n default text). `effective_config` mirrors it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from ulid import ULID

from app.database.models.compliance_notice_config import NOTICE_SURFACES, ComplianceNoticeConfig
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import sealing

LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
MAX_TEXT_CHARS = 2000
MAX_LOCALES = 32
MAX_RATIONALE_CHARS = 4000

EXEMPTION_BASES = frozenset({
    "obvious_from_context",  # Art 50(1): obvious to a reasonably well-informed person
    "authorised_by_law",  # Art 50(1): detection/prevention/investigation of offences
    "no_natural_person",  # the system does not interact with natural persons
    "other",
})

DEFAULT_CONFIG: Dict[str, Any] = {"enabled": True, "surface": "both", "texts": {}, "text_digests": {}, "exemption": None}


class NoticeError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class NoticeSave:
    config: ComplianceNoticeConfig
    seal: sealing.SealOutcome
    declaration: Optional[sealing.SealOutcome]


def _validate_system(db: Session, tenant_id: str, ai_system_id: Optional[str]) -> Optional[str]:
    if not ai_system_id:
        return None
    exists = (
        db.query(ResourceGroup.id)
        .filter(
            ResourceGroup.id == ai_system_id,
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        .first()
    )
    if exists is None:
        raise NoticeError(f"AI System {ai_system_id!r} not found", 404)
    return ai_system_id


def validate_texts(texts: Mapping[str, str]) -> Dict[str, str]:
    if len(texts) > MAX_LOCALES:
        raise NoticeError(f"at most {MAX_LOCALES} locales")
    out: Dict[str, str] = {}
    for locale, text in texts.items():
        if not isinstance(locale, str) or not LOCALE_RE.match(locale):
            raise NoticeError(f"locale {locale!r} is not a BCP 47 tag (e.g. en, sv, pt-BR)")
        if not isinstance(text, str) or not text.strip():
            raise NoticeError(f"text for {locale!r} is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise NoticeError(f"text for {locale!r} exceeds {MAX_TEXT_CHARS} characters")
        out[locale] = text
    return out


def text_digests(texts: Mapping[str, str]) -> Dict[str, str]:
    """`{locale: hex SHA-256 of the UTF-8 text}` — byte-for-byte what the
    portal hashes before it posts `text_sha256`."""
    return {locale: sealing.text_digest(text) for locale, text in texts.items()}


def validate_exemption(exemption: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not exemption:
        return None
    asserted = bool(exemption.get("asserted"))
    if not asserted:
        return None
    basis = exemption.get("basis")
    if basis not in EXEMPTION_BASES:
        raise NoticeError(f"exemption.basis must be one of {sorted(EXEMPTION_BASES)}")
    rationale = (exemption.get("rationale") or "").strip()
    if not rationale:
        raise NoticeError("an asserted exemption needs a rationale")
    if len(rationale) > MAX_RATIONALE_CHARS:
        raise NoticeError(f"exemption.rationale exceeds {MAX_RATIONALE_CHARS} characters")
    return {"asserted": True, "basis": basis, "rationale": rationale}


def get_config(db: Session, tenant_id: str, ai_system_id: Optional[str]) -> Optional[ComplianceNoticeConfig]:
    q = db.query(ComplianceNoticeConfig).filter(ComplianceNoticeConfig.tenant_id == tenant_id)
    if ai_system_id:
        q = q.filter(ComplianceNoticeConfig.ai_system_id == ai_system_id)
    else:
        q = q.filter(ComplianceNoticeConfig.ai_system_id.is_(None))
    return q.first()


def effective_config(db: Session, tenant_id: str, ai_system_id: Optional[str]) -> Dict[str, Any]:
    """The configuration the portal resolves for a system, with where it
    came from (`system | tenant | default`)."""
    if ai_system_id:
        row = get_config(db, tenant_id, ai_system_id)
        if row is not None:
            return {**row.to_dict(), "source": "system"}
    row = get_config(db, tenant_id, None)
    if row is not None:
        return {**row.to_dict(), "source": "tenant"}
    return {**DEFAULT_CONFIG, "tenant_id": tenant_id, "ai_system_id": None, "record_id": None, "source": "default"}


def _seal_config(row: ComplianceNoticeConfig, *, state: str) -> List[Dict[str, Any]]:
    digests = row.text_digests or {}
    refs = sealing.references(
        *(
            sealing.reference("ai.brutor.notice-text", digests[loc], "declared_text")
            for loc in sorted(digests)
        )
    )
    return [
        sealing.build_record(
            sealing.RECORD_NOTICE_CONFIG,
            ai_system_id=row.ai_system_id,
            context={"state": state, "tags": sorted(digests)[:64]},
            payload={
                "config_id": row.id,
                "ai_system_id": row.ai_system_id,
                "enabled": bool(row.enabled),
                "surface": row.surface,
                "text_digests": digests,
                "exemption": _exemption_payload(row.exemption),
                "state": state,
            },
            references=refs,
        )
    ]


def _exemption_payload(exemption: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not exemption:
        return None
    return {k: exemption.get(k) for k in ("asserted", "basis", "rationale")}


def save_config(
    db: Session,
    tenant_id: str,
    *,
    ai_system_id: Optional[str],
    enabled: bool,
    surface: str,
    texts: Mapping[str, str],
    exemption: Optional[Mapping[str, Any]],
    actor: Optional[str],
) -> NoticeSave:
    """Create or replace the config for one scope; seal it (and the
    exemption declaration when one is newly asserted or changed)."""
    ai_system_id = _validate_system(db, tenant_id, ai_system_id)
    if surface not in NOTICE_SURFACES:
        raise NoticeError(f"surface must be one of {sorted(NOTICE_SURFACES)}")
    clean_texts = validate_texts(texts or {})
    clean_exemption = validate_exemption(exemption)

    row = get_config(db, tenant_id, ai_system_id)
    previous_exemption = _exemption_payload(row.exemption) if row is not None else None
    previous_declaration_id = (row.exemption or {}).get("declaration_record_id") if row is not None else None
    if row is None:
        row = ComplianceNoticeConfig(
            id=f"ntc-{ULID()}".lower(),
            tenant_id=tenant_id,
            ai_system_id=ai_system_id,
        )
        db.add(row)
    row.enabled = bool(enabled)
    row.surface = surface
    row.texts = clean_texts
    row.text_digests = text_digests(clean_texts)
    row.exemption = dict(clean_exemption) if clean_exemption else None
    row.updated_by = actor
    for name in ("texts", "text_digests", "exemption"):
        flag_modified(row, name)
    db.flush()

    records = _seal_config(row, state="saved")
    declare = clean_exemption is not None and clean_exemption != previous_exemption
    if declare:
        records.append(
            sealing.build_record(
                sealing.RECORD_DECLARATION,
                ai_system_id=ai_system_id,
                context={"state": "notice_exemption_asserted"},
                payload={"declaration": "notice_exemption", "config_id": row.id, **clean_exemption},
            )
        )
    outcomes = sealing.seal_many(tenant_id, records)
    config_seal = outcomes[0]
    declaration = outcomes[1] if declare else None
    row.record_id = config_seal.record_id
    if row.exemption is not None:
        kept = dict(row.exemption)
        # An unchanged assertion keeps pointing at the declaration already sealed.
        kept["declaration_record_id"] = (
            (declaration.record_id if declaration else None) if declare else previous_declaration_id
        )
        row.exemption = kept
        flag_modified(row, "exemption")
    db.flush()
    return NoticeSave(config=row, seal=config_seal, declaration=declaration)


def delete_config(db: Session, row: ComplianceNoticeConfig) -> sealing.SealOutcome:
    """Remove a scope's config (the system falls back to the tenant row or
    the default). Sealed, so a removed notice is on the record too."""
    records = _seal_config(row, state="deleted")
    outcome = sealing.seal_many(row.tenant_id, records)[0]
    db.delete(row)
    db.flush()
    return outcome


__all__ = [
    "DEFAULT_CONFIG",
    "EXEMPTION_BASES",
    "NoticeError",
    "NoticeSave",
    "delete_config",
    "effective_config",
    "get_config",
    "save_config",
    "text_digests",
    "validate_exemption",
    "validate_texts",
]
