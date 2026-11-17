# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The real entry points of the control plane, each run in a process of its
own behind the suite's walls.

`run(...)` starts `python -c <bootstrap>`: the bootstrap puts the walls of
`tests/hermetic.py` up in the child (no Docker daemon, no cluster, no
network), takes the developer's `.env` out of reach (an entry point loads
the one beside the sources; a test must not depend on what a developer
keeps there), and then runs the entry point exactly as its command line
would: `main.py`, `enterprise/main.py`, the `alembic` command, or
`gunicorn.conf.py` as Gunicorn reads it.

The child gets the environment it is given and nothing else of the
parent's but `PATH`: what a test wants set, it sets.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]

# What every start needs. The database is a port nothing listens on.
BASE_ENVIRONMENT = {
    "DATABASE_URL": "postgresql://nobody:nothing@127.0.0.1:1/none",
    "JWT_SECRET": "entry-point-test-jwt-secret-0123456789abcdef",
    "ENCRYPTION_KEY": "entry-point-test-encryption-key-0123456789ab",
}

_BOOTSTRAP = r"""
import runpy, sys
repo, kind, target = sys.argv[1], sys.argv[2], sys.argv[3]
arguments = sys.argv[4:]
sys.path.insert(0, repo)
from tests import hermetic
hermetic.install()
import dotenv
dotenv.find_dotenv = lambda *a, **k: ""
dotenv.load_dotenv = lambda *a, **k: False
if kind == "script":
    sys.argv = [target, *arguments]
    runpy.run_path(target, run_name="__main__")
elif kind == "alembic":
    from alembic.config import main
    sys.argv = ["alembic", *arguments]
    main(argv=arguments)
elif kind == "gunicorn-config":
    sys.argv = ["gunicorn", *arguments]
    runpy.run_path(target, run_name="__config__")
    print("gunicorn configuration read")
"""


class Finished:
    def __init__(self, done: subprocess.CompletedProcess) -> None:
        self.status = done.returncode
        self.stdout = done.stdout
        self.stderr = done.stderr
        self.output = done.stdout + done.stderr

    def refusal_lines(self) -> list:
        """The lines of the refusal: one per name."""
        lines = self.stderr.splitlines()
        if "ERROR: the environment is refused:" not in lines:
            return []
        start = lines.index("ERROR: the environment is refused:") + 1
        return [line.strip() for line in lines[start:] if line.startswith("  ")]


def run(
    kind: str,
    target: str,
    arguments: Sequence[str] = (),
    environment: Optional[Mapping[str, str]] = None,
    timeout: float = 120.0,
) -> Finished:
    """Run one entry point. `kind` is `script` (`target` is `main.py` or
    `enterprise/main.py`), `alembic` (the `alembic` command; `target` is
    ignored) or `gunicorn-config` (`target` is `gunicorn.conf.py`, read as
    Gunicorn reads it for the given command line)."""
    child: Dict[str, str] = {"PATH": os.environ.get("PATH", ""), **BASE_ENVIRONMENT, **(environment or {})}
    done = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP, str(REPO), kind, target, *arguments],
        cwd=str(REPO),
        env=child,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return Finished(done)
