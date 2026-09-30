"""WG-Guard Bot application package."""

from __future__ import annotations

import os

__version__ = "1.0.0"

#: Short revision of the source tree this image was built from.  ``install.sh``
#: and ``update.sh`` pass it as the ``GIT_COMMIT`` build arg; a plain
#: ``docker compose build`` leaves it empty.  It exists so that "which code is
#: actually running?" is answerable from the outside: ``/healthz`` reports it and
#: the startup log line prints it.
__commit__ = os.environ.get("WG_GUARD_COMMIT", "").strip()

__all__ = ["__commit__", "__version__"]
