"""Built-in provider adapters.

Importing this package registers every adapter via ``@register``.  To add a
backend, drop a module in this directory and add it to the imports below — see
``example.py`` for a fully documented template.
"""

from __future__ import annotations

from app.core.config import settings

# Importing a module is what runs its ``@register``.  Keep the list explicit (no
# dynamic discovery) so packaging tools and static analysis can see it.
from app.panels.providers import wgguard

if settings.is_production:
    # ``example`` is the documented template, not a backend: every method raises
    # NotImplementedError and the node cannot be reached.  It stays registered in
    # development and test (so the template keeps being exercised) but must not
    # appear in a production operator's "panel type" dropdown.
    __all__ = ["wgguard"]
else:
    from app.panels.providers import example

    __all__ = ["example", "wgguard"]
