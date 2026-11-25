"""Local AVR-X1400H additions to the entity catalogue (HA-free, like catalog.py).

Called from the `# LOCAL HOOK` at the end of catalog.build(). See LOCAL_MODS.md.
"""

from __future__ import annotations

from .avr import is_x1400h
from .catalog import EntityEntry


def adjust_catalog(device, known_keys, entries: list[EntityEntry]) -> list[EntityEntry]:
    """Add the local entities to the upstream catalogue (called at the end of build)."""

    if not (is_x1400h(device) or "select_amp_assign" in known_keys):
        return entries
    # The Web Control amp_assign select (newer receivers) would share this unique_id.
    entries = [e for e in entries if not (e.platform == "select" and e.key == "select_amp_assign")]
    entries.append(EntityEntry("select", "amp_assign_x1400h", "select_amp_assign", "speakers"))
    entries.append(EntityEntry("button", "amp_assign", "button_amp_assign_front_b", "speakers",
                               params={"mode": "front_b"}))
    entries.append(EntityEntry("button", "amp_assign", "button_amp_assign_front_height", "speakers",
                               params={"mode": "front_height"}))
    entries.append(EntityEntry("sensor", "amp_assign", "sensor_amp_assign", "speakers"))
    return entries
