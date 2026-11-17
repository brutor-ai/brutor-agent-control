# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Framework catalogs

One module per framework; each builds a `Framework` (see `base.py`) and
exposes it as module-level `FRAMEWORK`. Module name = registry id with `-`
→ `_` (`nist_ai_rmf` ↔ `nist-ai-rmf`); a static test pins that and that the
id exists in `compliance_frameworks`.

Modules are imported from an explicit list; one that does not exist yet is
simply absent from `CATALOGS` (an import error INSIDE a present module
still raises — a broken catalog must never silently disappear).
"""

from __future__ import annotations

import importlib
from typing import Dict, Optional

from app.services.compliance.catalogs.base import Framework

#: Explicit, ordered (= display order when the registry has no row).
CATALOG_MODULES = (
    "eu_ai_act",
    "iso_42001",
    "soc2",
    "hipaa",
    "gdpr",
    "dora",
    "nis2",
    "nist_ai_rmf",
    "colorado",
    "korea",
    "brazil",
)


def _load() -> Dict[str, Framework]:
    catalogs: Dict[str, Framework] = {}
    for name in CATALOG_MODULES:
        qualified = f"{__name__}.{name}"
        try:
            module = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name == qualified:
                continue
            raise
        framework = getattr(module, "FRAMEWORK")
        catalogs[framework.id] = framework
    return catalogs


#: registry id → catalog.
CATALOGS: Dict[str, Framework] = _load()


def get_catalog(framework_id: str) -> Optional[Framework]:
    return CATALOGS.get(framework_id)


__all__ = ["CATALOGS", "CATALOG_MODULES", "get_catalog"]
