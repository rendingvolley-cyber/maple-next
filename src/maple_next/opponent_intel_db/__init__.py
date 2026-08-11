"""Local, offline opponent usage-statistics database.

This package is structurally isolated from the battle UI/runtime: it is the
only place in the codebase permitted to perform network access, and it does
so only when the ``update-opponent-intel`` CLI subcommand (see ``cli.py``) is
invoked explicitly by the user, never during a battle session.

Importing this package (or any module inside it) must never perform network
access, filesystem writes, or any other side effect. Everything here is
side-effect-free at import time -- network calls only happen inside function
bodies that the CLI entrypoint invokes.

``src/maple_next/ui``, ``src/maple_next/application``, and
``src/maple_next/workers`` must never import from this package.
"""

from __future__ import annotations
