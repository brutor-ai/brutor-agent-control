# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Locate the Rust core repository from a Python test.

Several tests here assert that a Python constant, an Alembic migration and
the Rust implementation of the same rule agree — the secrets detector, the
residency check, the compliance tag catalogue. Those tests read Rust source
directly, so they need to know where the core is.

**Only `BRUTOR_CORE_REPO` says where it is.** There is no default: the core
is not assumed to sit beside this repository (in the public layout it does
not, and its directory has another name), and no other file of this
repository spells its path (`tests/test_core_repo.py` holds that).

* Not set (or empty): a test that reads the core is **skipped**, and the
  reason names the variable. A Community checkout on its own runs everything
  else. A read at import time is an error, not a skipped module: read the
  core inside a test or a fixture.
* Set to something that is not a directory: the test **fails**. A parity
  guard pointed at nothing must not pass by skipping.

**What is read is the core's committed HEAD, not its working tree.** Where
the core is a git checkout, the paths the tests read are exported from
`HEAD` once per run (`git archive`) and every path this module returns points
into that export. The working tree of a repository somebody is editing is
half of a change: a guard that reads it goes red and green with their
keystrokes, and compares this repository with a core that does not exist
yet. The commit is the core's own repository's, or, in the public
repository, the commit of the checkout this repository is a directory of
(`core-gateway/` beside `control-plane/`, `.git` at their root). Where the
core is not a git checkout (an unpacked source archive, also one unpacked
into some other repository) the tree is what there is and it is read as it
is. `core_revision()` says which
of the two was read, and which commit, for a failure message.
"""

from __future__ import annotations

import atexit
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

# The parts of the core the tests read. Exported from HEAD where they exist.
EXPORTED_PATHS = ("src", "enterprise/src", "tests/fixtures", "tests/migrations", "enterprise/tests/fixtures", "vectors")

# checkout -> (the directory to read, a description of what it is)
_resolved: Dict[Path, Tuple[Path, str]] = {}

# This repository's directory: the core is read at a commit of the checkout
# that holds both when it is a directory of the same repository (the public
# layout). A test points it at a temporary layout.
THIS_CHECKOUT = Path(__file__).resolve().parents[1]


CORE_REPO_ENV = "BRUTOR_CORE_REPO"


def core_checkout_or_none() -> Optional[Path]:
    """The directory `BRUTOR_CORE_REPO` names, or None when it is not set. A
    value that is not a directory fails the calling test."""
    value = os.environ.get(CORE_REPO_ENV, "").strip()
    if not value:
        return None
    path = Path(value)
    assert path.is_dir(), f"{CORE_REPO_ENV}={value} is not a directory: the core is not there"
    return path


def core_checkout_path() -> Path:
    """Where the core repository is: the directory `BRUTOR_CORE_REPO` names.
    Skips the calling test when the variable is not set."""
    path = core_checkout_or_none()
    if path is None:
        import pytest

        pytest.skip(
            f"{CORE_REPO_ENV} is not set: the core's source is not available, so this comparison "
            "with it is not made (set it to the core's checkout to make it)"
        )
    return path


def _git(checkout: Path, *args: str) -> Optional[bytes]:
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def _toplevel(directory: Path) -> Optional[Path]:
    top = _git(directory, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        return Path(top.decode().strip()).resolve()
    except OSError:
        return None


def _head_of(checkout: Path) -> Optional[Tuple[str, str, Path]]:
    """(the commit, the core's path inside it, the repository's top) when
    `checkout` is the top of a git work tree, or a directory of the work
    tree this repository is in (the public layout); `None` otherwise (not
    a repository, no commit yet, no git, or a plain directory inside some
    other repository)."""
    if not checkout.is_dir():
        return None
    top = _toplevel(checkout)
    if top is None:
        return None
    try:
        here = checkout.resolve()
    except OSError:
        return None
    if top == here:
        prefix = ""
    elif top == _toplevel(THIS_CHECKOUT) and top in here.parents:
        prefix = here.relative_to(top).as_posix() + "/"
    else:
        return None
    head = _git(checkout, "rev-parse", "HEAD")
    return (head.decode().strip(), prefix, top) if head else None


def _export_head(top: Path, head: str, prefix: str) -> Optional[Path]:
    """The core's paths at `head`, from the repository whose top is `top`;
    `prefix` is the core's directory in it ("" where the core is the top).
    Run from the top: from a subdirectory, git reads paths relative to it."""
    present = [p for p in EXPORTED_PATHS if _git(top, "cat-file", "-e", f"{head}:{prefix}{p}") is not None]
    if not present:
        return None
    tree = f"{head}:{prefix.rstrip('/')}" if prefix else head
    archive = _git(top, "archive", "--format=tar", tree, *present)
    if archive is None:
        return None
    target = Path(tempfile.mkdtemp(prefix="brutor-core-head-"))
    atexit.register(shutil.rmtree, target, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    return target


def _resolve() -> Tuple[Path, str]:
    checkout = core_checkout_path()
    cached = _resolved.get(checkout)
    if cached is not None:
        return cached
    found = _head_of(checkout)
    exported = _export_head(found[2], found[0], found[1]) if found else None
    if exported is not None:
        head, prefix, top = found
        where = "its committed HEAD" if not prefix else f"the committed HEAD of {top}"
        result = (exported, f"{checkout} at {where}, commit {head} (the working tree is not read)")
    else:
        result = (checkout, f"the files in {checkout} as they are (not a git checkout: there is no commit to read)")
    _resolved[checkout] = result
    return result


def core_repo_path() -> Path:
    """Root of the core source the tests read: an export of the checkout's
    committed HEAD, or the checkout itself where it is not a git repository."""
    return _resolve()[0]


def core_revision() -> str:
    """What `core_repo_path()` is, for a failure message."""
    return _resolve()[1]


def core_source(*parts: str) -> Path:
    """Path to a file under the core repo's `src/` tree."""
    return core_repo_path().joinpath("src", *parts)
