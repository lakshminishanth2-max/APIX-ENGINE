"""Settings package.

``DJANGO_SETTINGS_MODULE`` selects ``config.settings.local`` or
``config.settings.production``; importing this package directly yields the
shared base so ``python -c "import config.settings"`` stays cheap.
"""

from __future__ import annotations
