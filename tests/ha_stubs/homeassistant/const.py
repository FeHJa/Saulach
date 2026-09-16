"""Fake of the handful of homeassistant.const symbols saulach imports."""

from __future__ import annotations

from enum import StrEnum

__version__ = "2026.8.0"

STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"


class EntityCategory(StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"
