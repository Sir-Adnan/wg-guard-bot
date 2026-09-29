"""Built-in provider adapters.

Importing this package registers every adapter via ``@register``.  To add a
backend, drop a module in this directory and add one import line below — see
``example.py`` for a fully documented template.
"""

from __future__ import annotations

# Importing the modules is what runs ``@register``.  Keep the list explicit (no
# dynamic discovery) so packaging tools and static analysis can see it.
# Registered for documentation purposes only; its methods raise
# NotImplementedError until someone implements them.  Remove this import once a
# real second adapter exists.
from app.panels.providers import (
    example,
    wgguard,
)

__all__ = ["example", "wgguard"]
