# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Probe: what does the Community composition load?

Run as a script in a process of its own (`tests/test_community_import_probe.py`
does), never imported by the test suite: it looks at `sys.modules`, and in
`block` mode it makes imports fail.

    python tests/community_import_probe.py record <boundary.json>
    python tests/community_import_probe.py block  <boundary.json>
    python tests/community_import_probe.py block-selftest <boundary.json>

`<boundary.json>` holds `{"modules": [dotted names], "libraries": [names]}`:
the Enterprise modules of this tree (a package stands for everything under
it) and the third-party libraries only Enterprise code uses.

Both modes do what a Community server does up to serving: import the
application module, build the application (`app.app.create_app`, which is
the Community Edition) and its OpenAPI document.

* `record`: nothing is blocked; prints which of the names ended up loaded.
* `block`: importing any of the names raises `ModuleNotFoundError`, as it
  would in a Community build that does not ship them; prints whether the
  application was built, and if not, the refused import that stopped it and
  the file that asked for it.

The last line of output is one JSON object, after the marker `PROBE_RESULT`.
"""

from __future__ import annotations

import importlib.abc
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_listed(name: str, modules, libraries) -> bool:
    if name.split(".")[0] in libraries:
        return True
    return any(name == m or name.startswith(m + ".") for m in modules)


class _Refuse(importlib.abc.MetaPathFinder):
    def __init__(self, modules, libraries) -> None:
        self.modules, self.libraries = modules, libraries
        self.refused = []

    def find_spec(self, name, path, target=None):
        if _is_listed(name, self.modules, self.libraries):
            self.refused.append(name)
            raise ModuleNotFoundError(f"not in a Community build: {name}", name=name)
        return None


def _start() -> None:
    import app.app  # noqa: F401

    application = app.app.create_app()
    application.openapi()


def _start_importing_enterprise() -> None:
    """For the probe's own check (`block-selftest`): a start that does import
    an Enterprise module, which the block must stop."""
    _start()
    import brutor_enterprise.licence.features  # noqa: F401


def main(mode: str, boundary_path: str) -> dict:
    boundary = json.loads(Path(boundary_path).read_text(encoding="utf-8"))
    modules, libraries = boundary["modules"], boundary["libraries"]
    sys.path.insert(0, str(ROOT))
    # The Enterprise package is there to be found, as in a development tree:
    # a Community start must not ask for it, and `block` refuses it.
    sys.path.insert(0, str(ROOT / "enterprise"))
    os.chdir(ROOT)

    finder = None
    start = _start_importing_enterprise if mode == "block-selftest" else _start
    if mode in ("block", "block-selftest"):
        finder = _Refuse(modules, libraries)
        sys.meta_path.insert(0, finder)
    elif mode != "record":
        raise SystemExit(f"unknown mode: {mode}")

    result = {"mode": mode, "starts": True, "first_refused": None, "asked_by": None, "error": None}
    try:
        start()
    except BaseException as exc:  # noqa: BLE001 - reported, not handled
        result["starts"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        # The refusal that stopped the start is the last one made: an earlier
        # one that the importing code tolerated (try/except) did not stop it.
        if finder is not None and isinstance(exc, ImportError) and finder.refused:
            result["first_refused"] = finder.refused[-1]
            frames = [
                frame
                for frame in traceback.extract_tb(exc.__traceback__)
                if frame.filename.startswith(str(ROOT)) and "importlib" not in frame.filename
            ]
            askers = [f for f in frames if not f.filename.endswith("community_import_probe.py")]
            if askers:
                result["asked_by"] = os.path.relpath(askers[-1].filename, ROOT)
    loaded = sorted(sys.modules)
    result["enterprise_modules_loaded"] = [n for n in loaded if _is_listed(n, modules, ())]
    result["enterprise_libraries_loaded"] = [n for n in libraries if n in sys.modules]
    result["enterprise_package_loaded"] = [n for n in loaded if n.split(".")[0] == "brutor_enterprise"]
    return result


if __name__ == "__main__":
    outcome = main(sys.argv[1], sys.argv[2])
    print("PROBE_RESULT " + json.dumps(outcome, sort_keys=True))
