# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Entry point of the Brutor Agent Control control plane, COMMUNITY Edition.
The Enterprise Edition's is `enterprise/main.py`.

    python main.py [options]       run the server
    python main.py init            create the schema and seed a first install
    python main.py edition-reset [--keep-tenant ID] [--confirm-delete-enterprise-data]

The command line itself is `app/cli.py` (also the console script
`brutor-agent-control-plane`).
"""

from app.cli import dispatch, prepare

# Loading this module makes the process the Community Edition (the `.env`
# file is loaded and the Community application factory handed to the
# launcher) before any command runs.
prepare()


if __name__ == "__main__":
    dispatch()
