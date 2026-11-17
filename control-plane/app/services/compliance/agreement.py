# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Judge–human agreement statistics — pure functions

Stated beside every judged row: `JUDGED · met 57 of 60 · human agreement
0.92 [0.83–0.97], n = 34, blind`. Computed per `(check,
evaluator_fingerprint)` over the graded blind reviews of the last 30 days
(a fingerprint change resets the sample — a different judge is a different
instrument).

* **Observed agreement** — share of pairs where judge and human agree.
* **Cohen's κ** for categorical results (met / not_met / n/a) — agreement
  beyond chance; undefined (None) when chance agreement is 1 (both raters
  used one category only).
* **MAE** for numeric scores.
* **Wilson 95 % interval** on the observed agreement — honest at small n
  and at 0 / 1, unlike the normal approximation.
* **`insufficient_sample`** below 10 pairs: the numbers are still shown,
  but no calibration verdict is drawn from them.

No I/O, no clock: the caller supplies the pairs. Imported by the judge
resolvers and by the calibration / report code.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Below this many graded pairs no calibration verdict is drawn.
MIN_SAMPLE = 10
#: Observed agreement below this raises `judge_uncalibrated`.
CALIBRATION_THRESHOLD = 0.80
#: Agreement is computed over this many days of graded reviews.
AGREEMENT_WINDOW_DAYS = 30
#: z for a two-sided 95 % interval.
Z_95 = 1.959963984540054


def observed_agreement(pairs: Sequence[Tuple[Any, Any]]) -> Optional[float]:
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def cohens_kappa(pairs: Sequence[Tuple[Any, Any]]) -> Optional[float]:
    """κ = (p_o − p_e) / (1 − p_e). None when there are no pairs or when
    chance agreement is total (p_e = 1) — κ is undefined, not 1."""
    n = len(pairs)
    if n == 0:
        return None
    p_o = observed_agreement(pairs) or 0.0
    left = Counter(a for a, _ in pairs)
    right = Counter(b for _, b in pairs)
    p_e = sum((left[c] / n) * (right[c] / n) for c in set(left) | set(right))
    if math.isclose(p_e, 1.0):
        return None
    return (p_o - p_e) / (1.0 - p_e)


def mean_absolute_error(pairs: Sequence[Tuple[float, float]]) -> Optional[float]:
    numeric = [(float(a), float(b)) for a, b in pairs if a is not None and b is not None]
    if not numeric:
        return None
    return sum(abs(a - b) for a, b in numeric) / len(numeric)


def wilson_interval(successes: int, n: int, z: float = Z_95) -> Optional[Tuple[float, float]]:
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError("successes must be within [0, n]")
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 3)


def summarize(
    categorical: Iterable[Tuple[Any, Any]] = (),
    numeric: Iterable[Tuple[Optional[float], Optional[float]]] = (),
    *,
    blind: bool = True,
    min_sample: int = MIN_SAMPLE,
) -> Dict[str, Any]:
    """The agreement block shown beside a judged row."""
    cat: List[Tuple[Any, Any]] = [(a, b) for a, b in categorical if a is not None and b is not None]
    num = [(a, b) for a, b in numeric if a is not None and b is not None]
    n = len(cat)
    agree = sum(1 for a, b in cat if a == b)
    ci = wilson_interval(agree, n)
    obs = observed_agreement(cat)
    insufficient = n < min_sample
    return {
        "n": n,
        "agreements": agree,
        "observed_agreement": _r(obs),
        "kappa": _r(cohens_kappa(cat)),
        "ci95": [_r(ci[0]), _r(ci[1])] if ci else None,
        "mae": _r(mean_absolute_error(num)),
        "numeric_n": len(num),
        "blind": blind,
        "insufficient_sample": insufficient,
        # Only a sufficient sample can call a judge uncalibrated.
        "below_threshold": (not insufficient) and obs is not None and obs < CALIBRATION_THRESHOLD,
        "threshold": CALIBRATION_THRESHOLD,
    }


def render(block: Dict[str, Any]) -> str:
    """`human agreement 0.92 [0.83–0.97], n = 34, blind` (or the
    insufficient-sample line)."""
    if not block or not block.get("n"):
        return "no human grades yet"
    ci = block.get("ci95") or [None, None]
    text = (
        f"human agreement {block['observed_agreement']:.2f} "
        f"[{ci[0]:.2f}–{ci[1]:.2f}], n = {block['n']}"
    )
    if block.get("blind"):
        text += ", blind"
    if block.get("insufficient_sample"):
        text += " (insufficient sample)"
    return text


__all__ = [
    "AGREEMENT_WINDOW_DAYS",
    "CALIBRATION_THRESHOLD",
    "MIN_SAMPLE",
    "cohens_kappa",
    "mean_absolute_error",
    "observed_agreement",
    "render",
    "summarize",
    "wilson_interval",
]
