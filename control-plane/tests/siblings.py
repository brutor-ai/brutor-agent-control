# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Where the other components are, and where git is: the one place a test of
this repository looks for either.

This repository is in one of two layouts:

* its own repository, with the other components' repositories beside it
  under their repository names (`../brutor-gateway-admin`), and `.git` here;
* the public repository, one monorepo, where this directory is
  `control-plane/` and the other components are its sibling directories
  (`../admin-console`), and `.git` is at the repository's root above.

A sibling is looked for under its repository's name, then under its
directory in the public repository; the first that is a directory is the
sibling. Where neither is, the test that reads it is skipped, and the skip
names both places. Where the sibling is there and the file the test reads
is not, the test fails: a moved file would otherwise stop being checked.

The core is not looked for here: it is where `BRUTOR_CORE_REPO` says, and
only there (`tests/core_repo.py`); the variable wins alone.

The git root is the nearest directory, this one or above, that holds
`.git` (a directory, or a file for a worktree). A test that asks git what
is tracked runs where there is one, in either layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]

# This repository's two names: its repository's, and its directory in the
# public repository.
THIS: Tuple[str, str] = ("brutor-gateway-control", "control-plane")

# Each sibling a test reads: its repository's name, then its directory in the
# public repository (the cut's layout.json; the cut suite holds the two).
SIBLINGS: Dict[str, Tuple[str, str]] = {
    "admin-console": ("brutor-gateway-admin", "admin-console"),
}


def places(name: str, repo: Path = REPO) -> List[Path]:
    """Where the sibling `name` is looked for beside `repo`, in order."""
    if name not in SIBLINGS:
        raise KeyError(f"no sibling is called {name!r}: one of {', '.join(sorted(SIBLINGS))}")
    return [Path(repo).parent / directory for directory in SIBLINGS[name]]


def sibling(name: str, repo: Path = REPO) -> Optional[Path]:
    """The sibling `name`: the first of its places that is a directory, or None."""
    return next((place for place in places(name, repo) if place.is_dir()), None)


def sibling_file(name: str, relative: str, repo: Path = REPO) -> Path:
    """The file `relative` of the sibling `name`. Skips the calling test when
    the sibling is in neither place (the reason names both); fails when the
    sibling is there and the file is not."""
    root = sibling(name, repo)
    if root is None:
        looked = " and at ".join(str(place / relative) for place in places(name, repo))
        pytest.skip(f"{name} is not beside this repository: {relative} was looked for at {looked}")
    path = root / relative
    assert path.is_file(), f"{name} is at {root} and has no {relative}: {path} does not exist"
    return path


def git_root(start: Path = REPO) -> Optional[Path]:
    """The nearest directory, `start` or above, that holds `.git`; None where
    there is none."""
    start = Path(start).resolve()
    return next((directory for directory in (start, *start.parents) if (directory / ".git").exists()), None)


def no_git_root(start: Path = REPO) -> str:
    """What a test that needs git says when `git_root(start)` is None."""
    return f"no .git at {Path(start).resolve()} or at any directory above it: what is tracked cannot be asked"


def public_root(repo: Path = REPO) -> Optional[Path]:
    """The public repository's root when `repo` is its `control-plane/`
    directory (the git root is the directory above, and this directory has
    the public name); None otherwise."""
    repo = Path(repo).resolve()
    root = git_root(repo)
    if root is not None and root == repo.parent and repo.name == THIS[1]:
        return root
    return None
