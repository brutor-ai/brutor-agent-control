# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The command line of the control plane.

    <entry> [options]        run the server (development: uvicorn)
    <entry> init             create the schema and seed a first install
    <entry> edition-reset    delete the Enterprise Edition's data
    <entry> admin-unlock     unlock an administrator (the one nobody else can unlock)
    <entry> --print-env      print every environment variable of the edition as JSON

`<entry>` is `python main.py` in a source tree or the console script
`brutor-agent-control-plane` of the installed Community package; both call
`main()` here. The Enterprise Edition's entry points
(`python enterprise/main.py`, `brutor-agent-control-plane-enterprise`) call
`brutor_enterprise.cli.main()`, which makes the process the Enterprise
Edition and then hands over to `dispatch()` here: one command line, two
editions, and the edition is the application factory that was handed over.

`edition-reset` lives here, in the Community package, because the operator
who needs it holds the Community Edition: it empties the Enterprise-only
tables of a database Enterprise has used so that Community starts on it. It
needs nothing of the Enterprise package (the list of those tables is
`app/composition/edition.py`). The Enterprise entry points offer it too, by
the same code.

This module imports nothing of the application at import time: the `.env`
file has to be loaded first (`app.database` reads `DATABASE_URL` when it is
imported).
"""

from __future__ import annotations

import sys
from typing import Any


def load_environment() -> None:
    """Load `.env`: the one in or above the working directory, else the one
    in or above the source tree."""
    from app import env_registry

    env_registry.load_dotenv_file()


def print_environment_registry(enterprise: bool) -> None:
    """`--print-env`: print the registry of the edition as JSON and exit.
    Nothing of the environment is read for it."""
    from app import env_registry, env_vars  # noqa: F401 - the table registers itself

    if env_registry.print_requested():
        sys.stdout.write(env_registry.REGISTRY.view(enterprise).json_text())
        sys.exit(0)


def check_environment(enterprise: bool) -> None:
    """Refuse the environment (exit status 1, one line per name, never a
    value) for a name this edition no longer reads or a value that is not
    of its variable's type. Called after `.env` is loaded and before
    anything else reads configuration or connects."""
    from app import env_registry, env_vars  # noqa: F401 - the table registers itself

    env_registry.refuse_or_exit(enterprise, missing=False)


def require_environment() -> None:
    """For a command that runs or installs the control plane: refuse it
    (exit status 1) when a required variable is not set. `edition-reset`,
    `admin-unlock` and `--help` serve nothing and ask for nothing here.

    The required variables of the Enterprise Edition are its own to ask
    for: its licence check, which runs before this, says more than a line
    here would."""
    from app import env_registry

    arguments = sys.argv[1:]
    if arguments[:1] in (["edition-reset"], ["admin-unlock"]) or "--help" in arguments or "--show-config" in arguments:
        return
    enterprise = env_registry.REGISTRY.holds_enterprise_variables()
    view = env_registry.REGISTRY.view(enterprise)
    env_registry.refuse_or_exit(
        enterprise,
        missing=True,
        reported_elsewhere=[var for var in view.vars if var.edition == env_registry.ENTERPRISE],
    )


def prepare() -> Any:
    """Make this process the Community Edition: load the environment and
    hand the Community application factory to the launcher. Returns the
    launcher module."""
    print_environment_registry(enterprise=False)
    load_environment()
    check_environment(enterprise=False)
    from app import launcher
    from app.app import create_app

    launcher.launch(launcher.Edition(create_app=create_app))
    return launcher


def dispatch() -> None:
    """Run the command named on the command line, in the edition the entry
    point has launched."""
    from app import launcher

    require_environment()
    if len(sys.argv) > 1 and sys.argv[1] == 'init':
        # Remove 'init' from argv and call init command
        sys.argv.pop(1)
        launcher.init()
    elif len(sys.argv) > 1 and sys.argv[1] == 'edition-reset':
        # The operator command that deletes the Enterprise
        # Edition's data (a dry run unless confirmed on the command line).
        # Reached by its name here and by nothing else.
        sys.argv.pop(1)
        from app.services.edition.cli import edition_reset

        edition_reset()
    elif len(sys.argv) > 1 and sys.argv[1] == 'admin-unlock':
        # The operator's way to lift an administrator's lock: for the
        # installation whose only administrator is locked.
        sys.argv.pop(1)
        from app.services.admin_unlock import cli as admin_unlock_cli

        admin_unlock_cli.admin_unlock()
    else:
        # Default to run command
        launcher.run()


def main() -> None:
    """The Community Edition's command line."""
    prepare()
    dispatch()
