"""Local AVR-X1400H modifications of the device facade and the telnet parser.

Kept in this file (and local_entities.py / protocol_profile.local.json) so that
upstream releases merge without conflicts: upstream files only carry small
`# LOCAL HOOK` lines. See LOCAL_MODS.md at the repository root.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

import aiohttp

from .device import DenonAvrDevice
from .models import AvrState
from .protocol import commands
from .protocol.telnet_parser import TelnetParser

_LOGGER = logging.getLogger(__name__)

# Fixed volume scale like the core denonavr integration: volume_level = raw / 100
# (0.30 = 30 = -50 dB), independent of the MVMAX the receiver reports.
VOLUME_LEVEL_SCALE = 100.0

# mode: (SSPAAMOD token, label pushed back by the receiver)
AMP_ASSIGN = {
    "front_b": ("FRB", "Front B"),
    "front_height": ("FRH", "Front Height"),
}


def is_x1400h(device) -> bool:
    """The Amp Assign handling is specific to the AVR-X1400H."""

    return device.discovery.device.model_name == "AVR-X1400H"


class LocalTelnetParser(TelnetParser):
    """Telnet parser with the local quick select, source and Amp Assign handling."""

    def _dispatch(self, line: str, state: AvrState) -> bool:
        if line.startswith("MSQUICK"):
            token = line[7:].strip()
            if not token.isdigit():
                return False
            number = int(token)
            # AVR-X1400H reports MSQUICK0 even after recalling a preset. Once Home
            # Assistant recalled one, keep it instead of clearing it with that reply.
            if number == 0 and state.values.get("quick_select"):
                return False
            state.values["quick_select"] = number
            return True
        if line.startswith("SI"):
            # A real source change clears the input mode and the quick select
            # (unless a quick select recall is still pending).
            source = line[2:].strip()
            zone = state.zone("main")
            if source != zone.source:
                zone.source = source
                state.values.pop("input_mode", None)
                pending_until = state.values.pop("quick_select_pending_until", 0)
                if monotonic() > pending_until:
                    state.values.pop("quick_select", None)
            return True
        if line.startswith("SSPAAMOD"):
            label = {"FRB": "Front B", "FRH": "Front Height"}.get(line[8:].strip())
            if label is None:
                return False
            state.values["amp_assign"] = label
            return True
        return super()._dispatch(line, state)


class LocalDenonAvrDevice(DenonAvrDevice):
    """DenonAvrDevice with the local AVR-X1400H behaviour."""

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        super().__init__(session, host)
        # Swap in the local parser where the facade wired the upstream one.
        for owner in (self._sync, self._discovery_service):
            if not hasattr(owner, "_parser"):
                raise RuntimeError(f"denon_avr local mods: {type(owner).__name__} has no _parser (upstream changed)")
        parser = LocalTelnetParser(self._profile, self._discovery)
        self._sync._parser = parser
        self._discovery_service._parser = parser
        # The menu commands are stateful: serialize Amp Assign changes.
        self._amp_assign_lock = asyncio.Lock()

    # Volume: fixed scale (raw / 100) ------------------------------------------

    async def async_set_volume_level(self, zone_id: str, level: float) -> None:
        level = max(0.0, min(1.0, level))
        await self._control.set_volume_raw(zone_id, round(level * VOLUME_LEVEL_SCALE * 2) / 2)

    # Sound mode: Dolby Surround -----------------------------------------------

    async def async_select_sound_mode(self, name: str) -> None:
        if name == "Dolby Surround":
            await self._select_dolby_surround(name)
            return
        await super().async_select_sound_mode(name)

    async def _select_dolby_surround(self, name: str) -> None:
        """AVR-X1400H reports MSDOLBY SURROUND but ignores it as a setter.

        MOVIE cycles the receiver's selectable modes in this context, so press it
        until the receiver confirms Dolby Surround.
        """

        spec = self._profile.control("sound_mode")
        if spec is None or self._state.values.get("sound_mode_display") == name:
            return
        query = self._profile.introspection_query("current_sound_modes")
        movie = self._profile.sound_mode_genre_commands.get("MOV")
        if not query or not movie:
            raise RuntimeError("Cannot select Dolby Surround without MOVIE and OPSML")
        for _ in range(max(1, len(self._discovery.current_sound_modes))):
            self._state.values.pop("sound_mode_display", None)
            await self._telnet.async_send(f"{spec.prefix}{movie}")
            await asyncio.sleep(0.4)
            await self._telnet.async_send(query)
            for _ in range(12):
                selected = self._state.values.get("sound_mode_display")
                if selected:
                    break
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("Receiver did not confirm the selected sound mode")
            if selected == name:
                return
        raise RuntimeError("Dolby Surround was not selected by the receiver")

    # Quick select ---------------------------------------------------------------

    async def async_select_quick_select(self, number: int) -> None:
        number = int(number)
        self._state.values["quick_select_pending_until"] = monotonic() + 5
        try:
            await super().async_select_quick_select(number)
        except Exception:
            self._state.values.pop("quick_select_pending_until", None)
            raise
        # Some AVR models do not echo MSQUICK after a recall: publish the requested
        # preset now; a later push or poll stays authoritative.
        self._state.values["quick_select"] = number
        self._notifier.schedule()
        # A recall can change the speaker calibration and the Audyssey reference
        # offset, which the AVR-X1400H does not push: re-read them.
        self._sync._schedule_refresh("speaker_channel")
        self._sync._schedule_refresh("quick_select_audio")

    # Subwoofer / dialog level with their enable state ---------------------------

    async def async_set_control(self, control_id: str, value: object) -> None:
        if control_id not in {"subwoofer_level", "dialog_level"}:
            await super().async_set_control(control_id, value)
            return
        # These levels have a separate enable state (PSSWL/PSDIL ON/OFF); some
        # receivers ignore a level change while it is off.
        spec = self._profile.control(control_id)
        command = commands.control(self._profile, control_id, value)
        if spec is None or command is None:
            return
        if float(value) != 0:
            await self._telnet.async_send(f"{spec.prefix}ON")
            await self._telnet.async_send(command)
            return
        if control_id == "subwoofer_level":
            # The AVR can reject the zero level while adjustment is off.
            await self._telnet.async_send(f"{spec.prefix}ON")
        await self._telnet.async_send(command)
        await asyncio.sleep(0.25)
        await self._telnet.async_send(f"{spec.prefix}OFF")

    # Amp Assign (AVR-X1400H) ----------------------------------------------------

    def amp_assign_label(self) -> str | None:
        """The current Amp Assign mode (Front B / Front Height).

        The SSPAAMOD push wins; before the first push the speaker setup tells it:
        Front Height is absent (NON) with Front B and configured with Front Height.
        """

        value = self._state.values.get("amp_assign")
        if value is not None:
            return str(value)
        front_height = self._state.speaker_sizes.get("FRH")
        if front_height == "NON":
            return "Front B"
        if front_height is not None:
            return "Front Height"
        return None

    async def _confirm_amp_assign_by_speaker_setup(self, label: str) -> bool:
        """Without an SSPAAMOD push, re-read the speaker setup (SSSPC): Front Height
        is absent (NON) with Front B and configured with Front Height."""

        await self._telnet.async_send("SSSPC ?")
        for _ in range(25):
            await asyncio.sleep(0.1)
            if self.amp_assign_label() == label:
                return True
        return False

    async def async_set_amp_assign(self, mode: str) -> None:
        """Set Amp Assign with the direct command SSPAAMOD <FRB|FRH>.

        Verified live on AVR-X1400H (2026-11-25): switches within 2 s, pushes
        SSPAAMOD and keeps the source (no on-screen menu). The confirmation is
        awaited (push, else the speaker setup); only without it the on-screen-menu macro
        runs. Front B then gets the A + B front speaker selection back (the change
        resets it) and the speaker values are re-read.
        """

        spec = AMP_ASSIGN.get(mode)
        if spec is None:
            raise ValueError(f"Unsupported Amp Assign mode: {mode}")
        token, label = spec
        use_menu = False
        async with self._amp_assign_lock:
            if self.amp_assign_label() != label:
                self._state.values.pop("amp_assign", None)
                await self._telnet.async_send(f"SSPAAMOD {token}")
                confirmed = False
                for _ in range(40):
                    await asyncio.sleep(0.1)
                    if self._state.values.get("amp_assign") == label:
                        confirmed = True
                        break
                if not confirmed:
                    confirmed = await self._confirm_amp_assign_by_speaker_setup(label)
                    if confirmed:
                        self._state.values["amp_assign"] = label
                        self._notifier.schedule()
                if not confirmed:
                    _LOGGER.warning("SSPAAMOD %s not confirmed; using the on-screen-menu fallback", token)
                    use_menu = True
        if use_menu:
            await self.async_set_amp_assign_from_menu(mode)
        if mode == "front_b":
            await asyncio.sleep(1.0)
            await self._control.set_control("front_speaker", "A+B")
        self._sync._schedule_refresh("speaker_channel")
        self._sync._schedule_refresh("quick_select_audio")
        self._sync._schedule_refresh("front_speaker")

    async def async_set_amp_assign_from_menu(self, mode: str) -> None:
        """Fallback: set Amp Assign through the on-screen-menu commands.

        The menu cursor is preserved after closing the setup screen, therefore the
        macro returns to the Audio root entry before it exits.
        """

        direction = {"front_b": "MNCLT", "front_height": "MNCRT"}.get(mode)
        if direction is None:
            raise ValueError(f"Unsupported Amp Assign mode: {mode}")
        menu: list[tuple[str, float]] = [
            ("MNMEN OFF", 0.35), ("MNOPT", 0.35), ("MNMEN OFF", 0.35),
            ("MNMEN ON", 0.35), ("MNCDN", 0.35), ("MNCDN", 0.35), ("MNCDN", 0.35),
            ("MNENT", 0.35), ("MNCDN", 0.35), ("MNENT", 0.35), ("MNENT", 0.35),
            ("MNENT", 0.5),  # the AVR needs longer to enter the editable option list
            (direction, 0.35), ("MNENT", 0.35), ("MNMEN OFF", 0.35),
        ]
        async with self._amp_assign_lock:
            for command, delay in menu:
                await self._telnet.async_send(command)
                await asyncio.sleep(delay)
        self._sync._schedule_refresh("speaker_channel")
        self._sync._schedule_refresh("quick_select_audio")
