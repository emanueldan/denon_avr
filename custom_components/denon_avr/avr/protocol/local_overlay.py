"""Local overlay for the protocol profile (protocol_profile.local.json).

The overlay is deep-merged over upstream's protocol_profile.json at load time, so
upstream's file stays untouched and merges cleanly. Dicts merge key by key; any
other value (lists, strings, numbers) replaces the upstream one.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .profile import _PROFILE_PATH, ProtocolProfile

_OVERLAY_PATH = Path(__file__).with_name("protocol_profile.local.json")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def merge_local_overlay(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the profile with the local overlay applied (unchanged without one)."""

    if not _OVERLAY_PATH.exists():
        return raw
    with _OVERLAY_PATH.open(encoding="utf-8") as handle:
        overlay = json.load(handle)
    overlay.pop("local_doc", None)
    return _deep_merge(raw, overlay)


@lru_cache(maxsize=1)
def load_local_profile() -> ProtocolProfile:
    """The bundled profile with the local overlay (used by LocalDenonAvrDevice only,
    so upstream's own tests keep running against upstream's profile)."""

    with _PROFILE_PATH.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return ProtocolProfile(merge_local_overlay(raw))
