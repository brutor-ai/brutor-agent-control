# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The outcome catalogue

A system declares how its runs may end: a list of outcomes, each optionally
with variants (domain states of the same outcome), each with whether it
counts toward the operating baseline and an optional `expect` — the evidence
a run reporting it must show (cheap outcomes need evidence).

This module is the one place the document is understood:

  * `validate_catalogue` — normalise or refuse (422 with field paths);
  * `judge` — the conformance of a reported id against a catalogue (the
    core judges at finalize; the control plane uses the same rule to read
    a run's evidence back and in tests);
  * `merged_expect` / `expect_violations` — the evidence;
  * `diff_catalogues` — widening / narrowing for the contract diff;
  * `changed_buckets` — the outcome buckets whose MEANING a contract change
    altered (an `expect` or the baseline flag), for baseline epochs;
  * `validate_expected_outcomes` — a dependency's `expected_outcomes`.

The normalised form is what the closure carries (section `outcomes`): sorted
by id, variants sorted, `expect` keys sorted, capability lists sorted and
de-duplicated, empty optional members omitted — two saves of the same
catalogue hash identically.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.constants.outcomes import (
    CONFORMANCE_DECLARED,
    CONFORMANCE_UNDECLARED,
    CONFORMANCE_UNREPORTED,
    DESCRIPTION_MAX,
    EXPECT_FORBIDS_CAPABILITIES,
    EXPECT_INT_KEYS,
    EXPECT_KEYS,
    EXPECT_LIST_KEYS,
    EXPECT_MAX_ACTIONS,
    EXPECT_MAX_COST_USD,
    EXPECT_MAX_KEYS,
    EXPECT_MAX_MODEL_CALLS,
    EXPECT_REQUIRES_CAPABILITIES,
    LABEL_MAX,
    MAX_OUTCOMES,
    MAX_VARIANTS,
    OUTCOME_ID_RE,
    OUTCOME_PART_RE,
)

Loc = Tuple[Any, ...]

_OUTCOME_KEYS = ("id", "label", "description", "counts_toward_baseline", "expect", "variants")
_VARIANT_KEYS = ("id", "label", "description", "expect")

#: The continuous metrics the core learns per outcome bucket —
#: each gets a `catalogue_change` epoch when its bucket's meaning changes.
BUCKETED_METRICS = (
    "action_count", "cost_per_run", "duration_ms", "tool_call_count",
    "peak_context_tokens", "context_growth",
)
#: The categorical metric over success runs' outcome ids: a change
#: of any baseline flag changes its population, so it re-learns on 'all'.
OUTCOME_MIX_METRIC = "outcome_mix"


class CatalogueValidationError(ValueError):
    """The document is not a valid catalogue. `errors` carries one
    `{"loc": [...], "msg": ...}` per problem, FastAPI-style, so the UI can
    put each message beside its field."""

    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        super().__init__("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.errors: List[Dict[str, Any]] = []

    def add(self, loc: Loc, msg: str) -> None:
        self.errors.append({"loc": list(loc), "msg": msg})


def _label(raw: Any, loc: Loc, errs: _Collector) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        errs.add(loc, "label is required (1–128 characters)")
        return None
    label = raw.strip()
    if len(label) > LABEL_MAX:
        errs.add(loc, f"label must be at most {LABEL_MAX} characters")
        return None
    return label


def _description(raw: Any, loc: Loc, errs: _Collector) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        errs.add(loc, "description must be a string")
        return None
    text = raw.strip()
    if len(text) > DESCRIPTION_MAX:
        errs.add(loc, f"description must be at most {DESCRIPTION_MAX} characters")
        return None
    return text or None


def _id(raw: Any, loc: Loc, errs: _Collector) -> Optional[str]:
    if not isinstance(raw, str) or not OUTCOME_PART_RE.match(raw):
        errs.add(loc, "id must match [a-z0-9_-]{1,48}")
        return None
    return raw


def _unknown_keys(obj: Dict[str, Any], allowed: Sequence[str], loc: Loc, errs: _Collector) -> None:
    for key in sorted(set(obj) - set(allowed)):
        errs.add(loc + (key,), f"unknown key {key!r}; allowed: {list(allowed)}")


def _expect(raw: Any, loc: Loc, errs: _Collector) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errs.add(loc, "expect must be an object")
        return None
    _unknown_keys(raw, EXPECT_KEYS, loc, errs)
    out: Dict[str, Any] = {}
    for key in EXPECT_KEYS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        here = loc + (key,)
        if key in EXPECT_INT_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errs.add(here, f"{key} must be a whole number ≥ 0")
                continue
            out[key] = int(value)
        elif key == EXPECT_MAX_COST_USD:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                errs.add(here, f"{key} must be a number ≥ 0")
                continue
            # Whole amounts as ints: 0 and 0.0 must hash identically.
            out[key] = int(value) if float(value).is_integer() else float(value)
        else:  # capability lists
            if not isinstance(value, list) or not all(
                isinstance(v, str) and v.strip() for v in value
            ):
                errs.add(here, f"{key} must be a list of capability names")
                continue
            names = sorted({v.strip() for v in value})
            if names:
                out[key] = names
    required = set(out.get(EXPECT_REQUIRES_CAPABILITIES, ()))
    forbidden = set(out.get(EXPECT_FORBIDS_CAPABILITIES, ()))
    both = sorted(required & forbidden)
    if both:
        errs.add(loc, f"capabilities both required and forbidden: {both}")
    return out or None


def _variant(raw: Any, loc: Loc, errs: _Collector) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        errs.add(loc, "a variant must be an object")
        return None
    _unknown_keys(raw, _VARIANT_KEYS, loc, errs)
    vid = _id(raw.get("id"), loc + ("id",), errs)
    label = _label(raw.get("label"), loc + ("label",), errs)
    description = _description(raw.get("description"), loc + ("description",), errs)
    expect = _expect(raw.get("expect"), loc + ("expect",), errs)
    if vid is None or label is None:
        return None
    out: Dict[str, Any] = {"id": vid, "label": label}
    if description:
        out["description"] = description
    if expect:
        out["expect"] = expect
    return out


def _outcome(raw: Any, loc: Loc, errs: _Collector) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        errs.add(loc, "an outcome must be an object")
        return None
    _unknown_keys(raw, _OUTCOME_KEYS, loc, errs)
    oid = _id(raw.get("id"), loc + ("id",), errs)
    label = _label(raw.get("label"), loc + ("label",), errs)
    description = _description(raw.get("description"), loc + ("description",), errs)
    counts = raw.get("counts_toward_baseline")
    if not isinstance(counts, bool):
        errs.add(loc + ("counts_toward_baseline",), "counts_toward_baseline must be true or false")
    expect = _expect(raw.get("expect"), loc + ("expect",), errs)
    variants_raw = raw.get("variants")
    variants: List[Dict[str, Any]] = []
    if variants_raw is not None:
        if not isinstance(variants_raw, list):
            errs.add(loc + ("variants",), "variants must be a list")
        else:
            if len(variants_raw) > MAX_VARIANTS:
                errs.add(loc + ("variants",), f"at most {MAX_VARIANTS} variants per outcome")
            seen: Dict[str, int] = {}
            for i, v in enumerate(variants_raw):
                norm = _variant(v, loc + ("variants", i), errs)
                if norm is None:
                    continue
                if norm["id"] in seen:
                    errs.add(loc + ("variants", i, "id"), f"duplicate variant id {norm['id']!r}")
                    continue
                seen[norm["id"]] = i
                variants.append(norm)
    if oid is None or label is None or not isinstance(counts, bool):
        return None
    out: Dict[str, Any] = {"id": oid, "label": label, "counts_toward_baseline": counts}
    if description:
        out["description"] = description
    if expect:
        out["expect"] = expect
    if variants:
        out["variants"] = sorted(variants, key=lambda v: v["id"])
    return out


def validate_catalogue(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """Normalise a catalogue or raise `CatalogueValidationError`.

    None or an empty list → None (no catalogue). See the module docstring
    for the normal form."""
    if raw is None:
        return None
    errs = _Collector()
    if not isinstance(raw, list):
        errs.add(("outcomes",), "the catalogue must be a list of outcomes")
        raise CatalogueValidationError(errs.errors)
    if len(raw) > MAX_OUTCOMES:
        errs.add(("outcomes",), f"at most {MAX_OUTCOMES} outcomes")
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for i, item in enumerate(raw):
        norm = _outcome(item, ("outcomes", i), errs)
        if norm is None:
            continue
        if norm["id"] in seen:
            errs.add(("outcomes", i, "id"), f"duplicate outcome id {norm['id']!r}")
            continue
        seen[norm["id"]] = i
        out.append(norm)
    if errs.errors:
        raise CatalogueValidationError(errs.errors)
    return sorted(out, key=lambda o: o["id"]) or None


def validate_expected_outcomes(raw: Any) -> Optional[List[str]]:
    """A dependency's `expected_outcomes`: None (not declared) or a sorted,
    de-duplicated list of `<outcome>[.<variant>]` ids. Raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ValueError("expected_outcomes must be a list of outcome ids")
    bad = [v for v in raw if not OUTCOME_ID_RE.match(v)]
    if bad:
        raise ValueError(
            f"expected_outcomes entries must match <outcome>[.<variant>] "
            f"([a-z0-9_-]{{1,48}} each): {bad}"
        )
    return sorted(set(raw))


# ---------------------------------------------------------------------------
# Judging a reported id — the conformance rule (mirrored by the core)
# ---------------------------------------------------------------------------


def _index(catalogue: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    return {o["id"]: o for o in (catalogue or []) if isinstance(o, dict) and "id" in o}


def find(catalogue: Optional[Iterable[Dict[str, Any]]], outcome_id: Optional[str]
         ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(outcome, variant) the id names when it is DECLARED, else (None, None).
    An outcome with variants must be reported with one; a bare outcome
    without variants must be reported bare."""
    if not outcome_id or not OUTCOME_ID_RE.match(outcome_id):
        return None, None
    oid, _, vid = outcome_id.partition(".")
    outcome = _index(catalogue).get(oid)
    if outcome is None:
        return None, None
    variants = {v["id"]: v for v in outcome.get("variants") or []}
    if variants:
        variant = variants.get(vid) if vid else None
        return (outcome, variant) if variant is not None else (None, None)
    return (outcome, None) if not vid else (None, None)


def judge(catalogue: Optional[Iterable[Dict[str, Any]]], outcome_id: Optional[str]
          ) -> Tuple[str, Optional[bool]]:
    """(conformance, counts_toward_baseline) of a reported id. Nothing
    reported ⇒ unreported; no catalogue or not declared ⇒ undeclared."""
    if not outcome_id:
        return CONFORMANCE_UNREPORTED, None
    outcome, _variant = find(catalogue, outcome_id)
    if outcome is None:
        return CONFORMANCE_UNDECLARED, None
    return CONFORMANCE_DECLARED, bool(outcome.get("counts_toward_baseline"))


def all_ids(catalogue: Optional[Iterable[Dict[str, Any]]]) -> List[str]:
    """Every reportable full id: `<outcome>` for an outcome without
    variants, `<outcome>.<variant>` for each variant. Sorted."""
    out: List[str] = []
    for o in catalogue or []:
        variants = o.get("variants") or []
        if variants:
            out.extend(f"{o['id']}.{v['id']}" for v in variants)
        else:
            out.append(o["id"])
    return sorted(out)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def merged_expect(catalogue: Optional[Iterable[Dict[str, Any]]], outcome_id: Optional[str]
                  ) -> Optional[Dict[str, Any]]:
    """The `expect` a run reporting `outcome_id` must meet: the variant's
    keys over the outcome's (variant wins). None when undeclared or empty."""
    outcome, variant = find(catalogue, outcome_id)
    if outcome is None:
        return None
    merged = dict(outcome.get("expect") or {})
    merged.update((variant or {}).get("expect") or {})
    return merged or None


def expect_violations(
    expect: Optional[Dict[str, Any]],
    *,
    action_count: int,
    llm_call_count: int,
    total_cost_usd: float,
    capabilities: Iterable[str],
) -> List[str]:
    """Which bounds of `expect` a run broke, as operator sentences. Empty
    when it met all of them (or there is nothing to meet)."""
    if not expect:
        return []
    caps = set(capabilities or ())
    out: List[str] = []
    if EXPECT_MAX_ACTIONS in expect and action_count > expect[EXPECT_MAX_ACTIONS]:
        out.append(f"{action_count} actions (expected at most {expect[EXPECT_MAX_ACTIONS]})")
    if EXPECT_MAX_MODEL_CALLS in expect and llm_call_count > expect[EXPECT_MAX_MODEL_CALLS]:
        out.append(
            f"{llm_call_count} model calls (expected at most {expect[EXPECT_MAX_MODEL_CALLS]})"
        )
    if EXPECT_MAX_COST_USD in expect and float(total_cost_usd or 0) > float(expect[EXPECT_MAX_COST_USD]) + 1e-12:
        out.append(
            f"cost ${float(total_cost_usd):.4f} (expected at most "
            f"${float(expect[EXPECT_MAX_COST_USD]):.4f})"
        )
    missing = sorted(set(expect.get(EXPECT_REQUIRES_CAPABILITIES) or ()) - caps)
    if missing:
        out.append(f"required capabilities not used: {missing}")
    used_forbidden = sorted(set(expect.get(EXPECT_FORBIDS_CAPABILITIES) or ()) & caps)
    if used_forbidden:
        out.append(f"forbidden capabilities used: {used_forbidden}")
    return out


# ---------------------------------------------------------------------------
# Contract diff (widening / narrowing)
# ---------------------------------------------------------------------------


def _diff_expect(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
                 where: str, entry_id: str, widened: list, narrowed: list) -> None:
    b, a = before or {}, after or {}

    def entry(kind: str, detail: str, key: str) -> Dict[str, Any]:
        return {"kind": kind, "section": "outcomes", "id": entry_id, "field": key,
                "detail": f"{where}: {detail}"}

    for key in EXPECT_MAX_KEYS:
        bv, av = b.get(key), a.get(key)
        if bv == av:
            continue
        if av is None:
            widened.append(entry("outcome_expect_loosened", f"{key} bound removed (was {bv})", key))
        elif bv is None:
            narrowed.append(entry("outcome_expect_tightened", f"{key} now bounded at {av}", key))
        elif av > bv:
            widened.append(entry("outcome_expect_loosened", f"{key} raised {bv} → {av}", key))
        else:
            narrowed.append(entry("outcome_expect_tightened", f"{key} lowered {bv} → {av}", key))
    for key in EXPECT_LIST_KEYS:
        bs, as_ = set(b.get(key) or ()), set(a.get(key) or ())
        removed, added = sorted(bs - as_), sorted(as_ - bs)
        verb = "required" if key == EXPECT_REQUIRES_CAPABILITIES else "forbidden"
        if removed:
            widened.append(entry(
                "outcome_expect_loosened", f"no longer {verb}: {removed}", key,
            ))
        if added:
            narrowed.append(entry(
                "outcome_expect_tightened", f"now {verb}: {added}", key,
            ))


def diff_catalogues(before: Optional[List[Dict[str, Any]]], after: Optional[List[Dict[str, Any]]],
                    widened: list, narrowed: list, changed: list) -> None:
    """Adding an outcome or variant widens, removing narrows;
    `counts_toward_baseline` true → false widens (it takes runs out of
    judgement — called out), false → true narrows; loosening an `expect`
    bound widens, tightening narrows; label / description edits are
    neither (reported as `changed`)."""
    b, a = _index(before), _index(after)
    for oid in sorted(a.keys() - b.keys()):
        o = a[oid]
        widened.append({
            "kind": "outcome_added", "section": "outcomes", "id": oid,
            "detail": (
                f"outcome {oid} ({o.get('label')}) declared"
                + ("" if o.get("counts_toward_baseline") else
                   " — does NOT count toward the baseline: runs ending there are out of judgement")
            ),
        })
    for oid in sorted(b.keys() - a.keys()):
        narrowed.append({
            "kind": "outcome_removed", "section": "outcomes", "id": oid,
            "detail": f"outcome {oid} ({b[oid].get('label')}) no longer declared",
        })
    for oid in sorted(a.keys() & b.keys()):
        bo, ao = b[oid], a[oid]
        bc, ac = bool(bo.get("counts_toward_baseline")), bool(ao.get("counts_toward_baseline"))
        if bc and not ac:
            widened.append({
                "kind": "outcome_baseline_flag", "section": "outcomes", "id": oid,
                "from": True, "to": False, "called_out": True,
                "detail": (
                    f"outcome {oid} no longer counts toward the baseline — its runs leave "
                    f"the success rate, the cost of the work and drift detection; read why"
                ),
            })
        elif ac and not bc:
            narrowed.append({
                "kind": "outcome_baseline_flag", "section": "outcomes", "id": oid,
                "from": False, "to": True,
                "detail": f"outcome {oid} now counts toward the baseline",
            })
        _diff_expect(bo.get("expect"), ao.get("expect"), f"outcome {oid} expect", oid,
                     widened, narrowed)
        for field in ("label", "description"):
            if (bo.get(field) or None) != (ao.get(field) or None):
                changed.append({
                    "kind": "outcome_text", "section": "outcomes", "id": oid, "field": field,
                    "detail": f"outcome {oid}: {field} changed",
                })
        bv = {v["id"]: v for v in bo.get("variants") or []}
        av = {v["id"]: v for v in ao.get("variants") or []}
        for vid in sorted(av.keys() - bv.keys()):
            widened.append({
                "kind": "outcome_variant_added", "section": "outcomes", "id": f"{oid}.{vid}",
                "detail": f"variant {oid}.{vid} ({av[vid].get('label')}) declared",
            })
        for vid in sorted(bv.keys() - av.keys()):
            narrowed.append({
                "kind": "outcome_variant_removed", "section": "outcomes", "id": f"{oid}.{vid}",
                "detail": f"variant {oid}.{vid} ({bv[vid].get('label')}) no longer declared",
            })
        for vid in sorted(av.keys() & bv.keys()):
            _diff_expect(bv[vid].get("expect"), av[vid].get("expect"),
                         f"variant {oid}.{vid} expect", f"{oid}.{vid}", widened, narrowed)
            for field in ("label", "description"):
                if (bv[vid].get(field) or None) != (av[vid].get(field) or None):
                    changed.append({
                        "kind": "outcome_text", "section": "outcomes", "id": f"{oid}.{vid}",
                        "field": field, "detail": f"variant {oid}.{vid}: {field} changed",
                    })


def diff_expected_outcomes(before: Optional[List[str]], after: Optional[List[str]], label: str,
                           entry, widened: list, narrowed: list) -> None:
    """A dependency's `expected_outcomes`: an added id widens (the caller now
    accepts an answer it did not), a removed one narrows."""
    b, a = set(before or ()), set(after or ())
    added, removed = sorted(a - b), sorted(b - a)
    if added:
        widened.append(entry(
            "dependency_expected_outcomes",
            f"{label}: now accepts delegate outcome(s) {added}",
            added=added,
        ))
    if removed:
        narrowed.append(entry(
            "dependency_expected_outcomes",
            f"{label}: no longer accepts delegate outcome(s) {removed}",
            removed=removed,
        ))


# ---------------------------------------------------------------------------
# Baseline epochs on catalogue change
# ---------------------------------------------------------------------------


def _buckets(catalogue: Optional[List[Dict[str, Any]]]) -> Dict[str, Tuple[bool, Optional[Dict[str, Any]]]]:
    """{bucket id: (counts_toward_baseline, merged expect)} — one bucket per
    reportable full id."""
    out: Dict[str, Tuple[bool, Optional[Dict[str, Any]]]] = {}
    for full in all_ids(catalogue):
        outcome, _ = find(catalogue, full)
        out[full] = (bool(outcome.get("counts_toward_baseline")), merged_expect(catalogue, full))
    return out


def changed_buckets(before: Optional[List[Dict[str, Any]]], after: Optional[List[Dict[str, Any]]]
                    ) -> Tuple[List[str], bool]:
    """(buckets whose meaning changed, whether any baseline flag changed).

    A bucket present on both sides whose merged `expect` or baseline flag
    differs has a new meaning: its per-outcome baselines must re-learn. A
    new bucket has no history and a removed one is no longer learned —
    neither needs an epoch."""
    b, a = _buckets(before), _buckets(after)
    changed = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
    flag_changed = any(a[k][0] != b[k][0] for k in a.keys() & b.keys())
    return changed, flag_changed


__all__ = [
    "BUCKETED_METRICS",
    "CatalogueValidationError",
    "OUTCOME_MIX_METRIC",
    "all_ids",
    "changed_buckets",
    "diff_catalogues",
    "diff_expected_outcomes",
    "expect_violations",
    "find",
    "judge",
    "merged_expect",
    "validate_catalogue",
    "validate_expected_outcomes",
]
