# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Regression guard — the app imports cleanly, without Authlib

Authlib stays out (dead code since 0.8.0), and the two crypto libraries the
app's wire formats depend on stay installed. `pyca/cryptography` is allowed
so `mcp` can move past 1.19 (see `pyproject.toml`). A crash in the child
process still fails here,
with its stderr.

# Why subprocess

We run the `import app.app` in a child Python process rather than the
current one. Two reasons:

  1. Realism — a production gunicorn worker starts from a fresh
     interpreter; reproducing that is the regression we care about.
  2. Isolation — clearing `sys.modules` in-process leaks fixtures
     between tests in the same pytest run (a2a_card_signing
     specifically caches its in-memory DB session in a module-level
     fixture; evicting `app.*` invalidates it). The subprocess
     approach has zero cross-test side effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# Probe script — imports app.app, then reports the cryptography /
# authlib / nacl / pycryptodome module state. Runs in a clean child
# Python interpreter so the result reflects real-world `python main.py`
# startup behaviour, not whatever modules our test runner has already
# pulled in.
_PROBE = """
import sys, json
import app.app  # the import gunicorn workers do at boot
authlib  = sorted(m for m in sys.modules if m.startswith('authlib'))
print(json.dumps({
    'authlib_modules': authlib,
    'nacl_loaded': any(m == 'nacl' or m.startswith('nacl.') for m in sys.modules),
    'pycryptodome_loaded': any(m.startswith('Crypto.') or m == 'Crypto' for m in sys.modules),
}))
"""


def _probe_app_import() -> dict:
    """Run the probe in a child Python, parse the JSON line it prints."""
    env = {
        **os.environ,
        # Match the env vars `conftest.py` sets so app.app can construct
        # without the operator-needed configuration nags.
        "DATABASE_URL": "postgresql://test:test@127.0.0.1:1/test_brutor",
        "JWT_SECRET": "test-jwt-secret-not-for-production",
        "ENCRYPTION_KEY": "a" * 44 + "=",
        # Ensure the child Python can find our `app/` package.
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        # If the child crashed, surface the stderr verbatim.
        raise AssertionError(
            f"`import app.app` in a clean subprocess failed with rc={result.returncode}.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n"
        )
    # The probe script prints exactly one JSON line on success;
    # anything else here is also failure information worth surfacing.
    last_line = result.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


def test_importing_app_does_not_load_authlib():
    # Authlib was removed in 0.8.0 as dead code.
    state = _probe_app_import()
    loaded = state["authlib_modules"]
    assert loaded == [], (
        "Authlib was removed in 0.8.0 as dead code; nothing in app/ "
        "imports it.\n\n"
        f"Loaded authlib modules: {loaded}"
    )


def test_replacement_libraries_are_available():
    # Affirmative version: the two libraries we DO use must be available.
    # Belt-and-braces against an accidental dep removal.
    import nacl.signing  # noqa: F401 — Ed25519 (card_signing.py)
    from Crypto.Cipher import AES  # noqa: F401 — AES (encryption_service.py)
    assert AES is not None
