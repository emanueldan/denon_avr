"""Local AVR-X1400H entities and entity tweaks.

Upstream platform files only call the factory helpers below from a `# LOCAL HOOK`
line (imported lazily inside async_setup_entry to avoid import cycles). Entity
unique_id keys are stable: never change them, dashboards and scripts use them.
See LOCAL_MODS.md at the repository root.
"""

from __future__ import annotations

from typing import Any, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory

from .catalog import EntityEntry  # noqa: F401  (factories receive EntityEntry)
from .coordinator import DenonAvrCoordinator
from .entity import DenonAvrEntity
from .avr import VOLUME_LEVEL_SCALE

Factories = dict[str, Callable[[EntityEntry], Any]]


# Platform factories ---------------------------------------------------------------

def button_factories(coordinator: DenonAvrCoordinator) -> Factories:
    return {"amp_assign": lambda e: DenonAvrAmpAssign(coordinator, e.params["mode"])}


def sensor_factories(coordinator: DenonAvrCoordinator) -> Factories:
    return {"amp_assign": lambda e: DenonAvrAmpAssignSensor(coordinator)}


def select_factories(coordinator: DenonAvrCoordinator, upstream: Factories) -> Factories:
    from .select import DenonAvrQuickSelect, DenonAvrSelect

    class LocalControlSelect(DenonAvrSelect):
        """input_mode: on the TV source with ARC/eARC active, offer only that mode."""

        @property
        def options(self) -> list[str]:
            if self._spec.id == "input_mode":
                state = self.coordinator.data
                if state.zone("main").source == "TV":
                    value = state.values.get("input_mode")
                    if value in {"ARC", "EARC"}:
                        label = self._value_to_label.get(value)
                        if label:
                            return [label]
            return self._attr_options

    class LocalQuickSelect(DenonAvrQuickSelect):
        """Without a known preset, derive one from the input mode."""

        @property
        def current_option(self) -> str | None:
            number = self.coordinator.data.values.get("quick_select")
            if number:
                selected = self._names.get(int(number))
                if selected:
                    return selected
            input_mode = self.coordinator.data.values.get("input_mode")
            default_name = {"DIGITAL": "Media", "ARC": "TV Audio", "AUTO": "Spotify"}.get(input_mode, "Media")
            options = self.options
            return default_name if default_name in options else (options[0] if options else None)

    return {
        "control": lambda e: LocalControlSelect(coordinator, e.params["spec"]),
        "quick_select": lambda e: LocalQuickSelect(coordinator),
        "amp_assign_x1400h": lambda e: DenonAvrAmpAssignSelect(coordinator),
    }


def media_player_factories(coordinator: DenonAvrCoordinator) -> Factories:
    from .media_player import DenonAvrMediaPlayer

    class LocalMediaPlayer(DenonAvrMediaPlayer):
        """Fixed volume scale (raw / 100) like the core denonavr integration."""

        @property
        def volume_level(self) -> float | None:
            zone = self._zone_state
            if zone.volume_raw is None:
                return None
            return max(0.0, min(1.0, zone.volume_raw / VOLUME_LEVEL_SCALE))

    return {"zone": lambda e: LocalMediaPlayer(coordinator, e.params["zone"])}


# Amp Assign entities (AVR-X1400H) -------------------------------------------------

class DenonAvrAmpAssignSelect(DenonAvrEntity, SelectEntity):
    """Amp Assign (Front B / Front Height) via the direct SSPAAMOD command.

    Choosing Front B also puts the front speakers back on A + B.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Amp Assign"
    _attr_icon = "mdi:speaker-multiple"
    _MODES = {"Front B": "front_b", "Front Height": "front_height"}

    def __init__(self, coordinator: DenonAvrCoordinator) -> None:
        super().__init__(coordinator, "select_amp_assign", sub_device="speakers")
        self._attr_options = list(self._MODES)

    @property
    def current_option(self) -> str | None:
        label = self.coordinator.device.amp_assign_label()
        return label if label in self._MODES else None

    async def async_select_option(self, option: str) -> None:
        mode = self._MODES.get(option)
        if mode is None:
            raise ServiceValidationError(f"Unsupported Amp Assign option: {option}")
        await self.coordinator.device.async_set_amp_assign(mode)
        self.coordinator.async_update_listeners()


class DenonAvrAmpAssign(DenonAvrEntity, ButtonEntity):
    """Set Amp Assign (direct command, on-screen-menu fallback)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:audio-input-stereo-minijack"

    def __init__(self, coordinator: DenonAvrCoordinator, mode: str) -> None:
        super().__init__(coordinator, f"button_amp_assign_{mode}", sub_device="speakers")
        self._mode = mode
        self._attr_name = f"Amp Assign {mode.replace('_', ' ').title()}"

    async def async_press(self) -> None:
        await self.coordinator.device.async_set_amp_assign(self._mode)


class DenonAvrAmpAssignSensor(DenonAvrEntity, SensorEntity):
    """The Amp Assign mode pushed by the AVR-X1400H (SSPAAMOD)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Amp Assign"

    def __init__(self, coordinator: DenonAvrCoordinator) -> None:
        super().__init__(coordinator, "sensor_amp_assign", sub_device="speakers")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.device.amp_assign_label()
