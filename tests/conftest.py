"""Test bootstrap.

Puts tests/ha_stubs on sys.path *before* anything imports `homeassistant`,
so `custom_components.saulach` (whose __init__.py is the real HA
integration entrypoint, importing homeassistant/voluptuous) can be
imported normally in this sandbox, which has no working real
`homeassistant` install matching our 2026.7 core floor. See
tests/ha_stubs/homeassistant/__init__.py and requirements_test.txt for
what this stub is and is not.

Also puts the repo root on sys.path so `custom_components.saulach`
resolves as a namespace package.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HA_STUBS_DIR = Path(__file__).resolve().parent / "ha_stubs"

for path in (str(_HA_STUBS_DIR), str(_REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
