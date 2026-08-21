"""Fake of the handful of homeassistant.const symbols saulach imports."""

from __future__ import annotations

from enum import StrEnum

__version__ = "2026.8.0"


class EntityCategory(StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"
