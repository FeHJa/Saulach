"""Reads this integration's own release version straight out of its
manifest.json, rather than duplicating it as a separate constant that could
drift out of sync (issue #12's metadata message needs it alongside the
protocol version)."""

from __future__ import annotations

import json
from pathlib import Path

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"


def integration_version() -> str:
    with _MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)["version"]
