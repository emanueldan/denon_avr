"""DenonAvrDevice: the public facade and composition root of the client library."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import partial
from time import monotonic

import aiohttp

from .models import AvrState, DeviceInfo, Discovery
from .protocol import commands
from .protocol.profile import ProtocolProfile, load_profile
from .protocol.telnet_parser import TelnetParser
from .services.control import ControlService
from .services.discovery import DiscoveryService
from .services.graphic_eq import GraphicEqController
from .services.runtime import BackgroundTasks, UpdateNotifier
from .services.sync import StateSync
from .services.staged import StagedChanges
from .services.webconfig_doc import WebConfigSectionController
from .transport import (
    GoformClient,
    HeosClient,
    TcpClient,
    TelnetClient,
    UpnpClient,
    WebConfigClient,
    async_probe,
)


_LOGGER = logging.getLogger(__name__)


class DenonAvrDevice:
    """Own, dependency light client for a Denon AVR over telnet and HTTP."""

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        self._host = host
        self._profile: ProtocolProfile = load_profile()
        self._discovery = Discovery()
        self._state = AvrState()
        self._available = False
        self._notifier = UpdateNotifier()
        self._tasks = BackgroundTasks()
        notify = self._notifier.schedule
        grammar = self._profile.grammar

        # Transports: one isolated client per wire protocol (see transport/).
        self._goform = GoformClient(session, host)
        self._upnp = UpnpClient(session, host)
        self._webconfig = WebConfigClient(session, host)
        # Web-config subsystems behind the ConfigDocumentPort.
        self._graphic_eq = GraphicEqController(
            self._webconfig,
            grammar.get("graphic_eq", {}),
            lambda: self._discovery.supports("GraphicEQ"),
            notify,
        )
        self._video_config = WebConfigSectionController(self._webconfig, grammar.get("video_web", {}), notify)
        self._speaker_config = WebConfigSectionController(self._webconfig, grammar.get("speakers_web", {}), notify)
        self._speaker_layout = StagedChanges(
            self._speaker_config,
            "speaker_layout_assign",
            self._read_control,
            self.async_set_control,
            lambda control_id: self._state.values.get(control_id),
        )

        # The persistent telnet connection is the CommandSink.
        self._telnet = TelnetClient(
            host,
            on_line=lambda line: self._sync.handle_line(line),
            on_connected=lambda: self._sync.resynchronise(),
            on_availability=self._handle_availability,
        )
        parser = TelnetParser(self._profile, self._discovery)
        self._sync = StateSync(
            self._profile,
            self._discovery,
            self._state,
            parser,
            self._telnet,
            self._tasks,
            notify,
            # Web sections a telnet event may trigger a re-read of (profile 'refresh').
            {
                "video": partial(self._refresh_section, self._video_config),
                "speakers": partial(self._refresh_section, self._speaker_config),
                "graphic_eq": self._graphic_eq.refresh,
            },
        )
        self._control = ControlService(
            self._profile,
            self._discovery,
            self._telnet,
            self._sync.refresh_current_sound_modes,
            self.volume_effective_max,
        )
        self._discovery_service = DiscoveryService(
            host,
            self._profile,
            self._discovery,
            self._state,
            parser,
            self._goform.async_get_device_info,
            self._upnp.async_get_description,
            partial(async_probe, host),
            (self._video_config, self._speaker_config),
        )
        # Auxiliary HEOS channel for network now-playing media and album art.
        self._heos = HeosClient(host, on_update=self._sync.handle_now_playing)
        # Local mod: the documented menu commands are stateful. Serialize Amp
        # Assign fallback macros so their navigation commands cannot overlap.
        self._amp_assign_menu_lock = asyncio.Lock()

    # Public properties ----------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def identifier(self) -> str:
        """Stable device identifier: MAC, else serial number, else host."""

        device = self._discovery.device
        return device.mac_address or device.serial_number or self._host

    @property
    def discovery(self) -> Discovery:
        return self._discovery

    @property
    def state(self) -> AvrState:
        return self._state

    @property
    def available(self) -> bool:
        return self._available

    @property
    def profile(self) -> ProtocolProfile:
        return self._profile

    # Master volume scale, preferring the values the receiver published in its Volume block and
    # falling back to the protocol defaults from the profile.
    @property
    def volume_reference(self) -> float:
        return self._discovery.volume.get("reference", self._profile.volume_reference)

    @property
    def volume_step(self) -> float:
        return self._discovery.volume.get("step", 0.5)

    @property
    def volume_absolute_max(self) -> float:
        return self._discovery.volume.get("absolute_max", self._profile.volume_max_fallback)

    def volume_effective_max(self, zone_id: str = "main") -> float:
        """The highest reachable raw volume: the receiver limit (MVMAX) or scale max."""

        raw_max = self._state.zone(zone_id).volume_max_raw
        return raw_max if raw_max else self.volume_absolute_max

    @property
    def volume_display(self) -> str:
        """The receiver's volume display mode; defaults to Relative (dB)."""

        return self._state.volume_display or "Relative"

    @property
    def configuration_url(self) -> str | None:
        """The receiver's setup web interface, once located (None before / when absent)."""

        endpoint = self._webconfig.endpoint
        return f"{endpoint.base_url}/" if endpoint is not None else None

    @property
    def web_reachable(self) -> bool | None:
        """Whether the setup web interface answered its last request (None: not tried)."""

        return self._webconfig.reachable

    @property
    def stats(self) -> dict[str, object]:
        """Diagnostics counters of the push/refresh machinery and the web documents (no payloads,
        no host)."""

        return {**self._sync.stats, "web_last_status": dict(self._webconfig.last_status)}

    @property
    def setup_interface(self) -> dict[str, object] | None:
        """How the setup web interface was located (diagnostics; no host)."""

        endpoint = self._webconfig.endpoint
        if endpoint is None:
            return None
        return {"scheme": endpoint.scheme, "port": endpoint.port, "source": endpoint.source}

    @property
    def speaker_layout(self) -> StagedChanges:
        """Staged edits of the Web Control speaker layout assign mode; nothing is written until
        ``async_apply`` (after the user confirmed)."""

        return self._speaker_layout

    @property
    def graphic_eq(self) -> GraphicEqController:
        """The manual graphic-EQ subsystem (read/edit/apply)."""

        return self._graphic_eq

    @property
    def video_config(self) -> WebConfigSectionController:
        """The /ajax video setup subsystem (HDMI setup, OSD, TV/4K format)."""

        return self._video_config

    @property
    def speaker_config(self) -> WebConfigSectionController:
        """The /ajax speaker setup subsystem (amp assign)."""

        return self._speaker_config

    def register_update_callback(self, callback: Callable[[], None]) -> None:
        """Register the single callback invoked on state/availability changes."""

        self._notifier.register(callback)

    # Lifecycle ------------------------------------------------------------

    async def async_fetch_identity(self) -> DeviceInfo:
        """Device identity only (config flow / SSDP); raises ConnectionError."""

        return await self._discovery_service.fetch_identity()

    async def async_discover(self) -> Discovery:
        """Populate the discovery model; raises ConnectionError when unreachable."""

        return await self._discovery_service.discover()

    async def async_start(self) -> None:
        """Start the telnet transport (which triggers the first resync)."""

        await self._telnet.async_start()
        # Best effort; the HEOS channel is auxiliary and self-heals on its own.
        await self._heos.async_start()

    async def async_await_ready(self, timeout: float = 8.0) -> None:
        """Best effort wait until the initial resync has populated state."""

        await self._discovery_service.await_ready(timeout)

    async def async_stop(self) -> None:
        """Stop the transports and cancel any pending notification or task."""

        self._speaker_layout.stop()
        self._notifier.cancel()
        self._sync.cancel()
        self._tasks.cancel_all()
        await self._telnet.async_stop()
        await self._heos.async_stop()

    async def async_poll(self) -> None:
        """HTTP reconciliation poll, used by the coordinator as a safety net."""

        await self._sync.async_reconcile(
            self._goform.async_get_status,
            lambda: self._telnet.connected,
            # Through the single-flight path, so a poll and an event trigger never read the same
            # section twice.
            tuple(partial(self._sync.refresh_web, s) for s in ("graphic_eq", "video", "speakers")),
        )

    async def _read_control(self, control_id: str) -> object | None:
        """A control's value as the receiver answers it NOW; None without an answer."""

        spec = self._profile.controls.get(control_id)
        if spec is None or not spec.query or not self._telnet.connected:
            return None
        if not await self._sync.async_answer(spec.query, spec.prefix):
            return None
        return self._state.values.get(control_id)

    async def _refresh_section(self, section: WebConfigSectionController) -> None:
        """Re-read a web section; menus that appear become features too."""

        await section.refresh()
        new = section.feature_flags() - self._discovery.features
        if not new:
            return
        self._discovery.features |= new
        for spec in self._profile.feature_controls():
            if spec.feature in new:
                for query in (spec.query, spec.get("list_query"), spec.get("read_query")):
                    if query:
                        await self._telnet.async_send(query)
        self._notifier.schedule()

    def _handle_availability(self, available: bool) -> None:
        self._available = available
        self._notifier.schedule()

    def _resync_queries(self) -> list[str]:
        """The resync query list (kept for the characterization tests)."""

        return self._sync.resync_queries()

    # Control (delegates to the ControlService) ------------------------------

    async def async_media_play(self) -> None:
        await self._heos.async_play()

    async def async_media_pause(self) -> None:
        await self._heos.async_pause()

    async def async_media_stop(self) -> None:
        await self._heos.async_stop_playback()

    async def async_media_next(self) -> None:
        await self._heos.async_next()

    async def async_media_previous(self) -> None:
        await self._heos.async_previous()

    async def async_set_power(self, zone_id: str, on: bool) -> None:
        await self._control.set_power(zone_id, on)

    async def async_set_system_power(self, on: bool) -> None:
        await self._control.set_system_power(on)

    async def async_set_volume_level(self, zone_id: str, level: float) -> None:
        await self._control.set_volume_level(zone_id, level)

    async def async_set_volume_raw(self, zone_id: str, raw: float) -> None:
        await self._control.set_volume_raw(zone_id, raw)

    async def async_volume_step(self, zone_id: str, up: bool) -> None:
        await self._control.volume_step(zone_id, up)

    async def async_set_mute(self, zone_id: str, muted: bool) -> None:
        await self._control.set_mute(zone_id, muted)

    async def async_select_source(self, zone_id: str, name: str) -> None:
        await self._control.select_source(zone_id, name)

    async def async_select_sound_mode(self, name: str) -> None:
        if name == "Dolby Surround":
            await self._select_dolby_surround(name)
            return
        await self._control.select_sound_mode(name)

    async def _select_dolby_surround(self, name: str) -> None:
        """Local mod: AVR-X1400H reports MSDOLBY SURROUND but ignores it as a setter.

        MOVIE cycles the receiver's selectable modes in this context, so press it
        until the receiver confirms Dolby Surround.
        """

        spec = self._profile.control("sound_mode")
        if spec is None:
            return
        if self._state.values.get("sound_mode_display") == name:
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

    async def async_select_quick_select(self, number: int) -> None:
        number = int(number)
        self._state.values["quick_select_pending_until"] = monotonic() + 5
        try:
            await self._control.select_quick_select(number)
        except Exception:
            self._state.values.pop("quick_select_pending_until", None)
            raise
        # Local mod: some AVR models do not echo MSQUICK after a recall. Publish
        # the successfully requested preset immediately; a later AVR push or poll
        # remains authoritative and can correct this state if needed.
        self._state.values["quick_select"] = number
        self._notifier.schedule()
        # Quick Select recalls can include a distinct speaker calibration and
        # Audyssey reference offset, but AVR-X1400H does not push these values.
        # Re-query them after the receiver has applied the preset.
        self._sync._schedule_refresh("speaker_channel")
        self._sync._schedule_refresh("quick_select_audio")

    # Local mod: AVR-X1400H Amp Assign -------------------------------------------
    _AMP_ASSIGN = {
        # mode: (SSPAAMOD token, label pushed back by the receiver, GET_AVRSTS value)
        "front_b": ("FRB", "Front B", "FrontB"),
        "front_height": ("FRH", "Front Height", "FrontHi"),
    }

    def amp_assign_label(self) -> str | None:
        """Local mod: the current Amp Assign mode as a label (Front B / Front Height).

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

    async def _read_amp_assign_1256(self) -> str | None:
        """Local mod: read the Amp Assign mode via GET_AVRSTS (port 1256, read only)."""

        client = TcpClient(self._host)
        try:
            await client.connect()
            setup = await client.async_read_setup()
            return (setup.get("status") or {}).get("AmpAssign")
        except Exception as err:  # noqa: BLE001 - verification only
            _LOGGER.debug("Amp Assign read over port 1256 failed: %s", err)
            return None
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    async def async_set_amp_assign(self, mode: str) -> None:
        """Local mod: set Amp Assign with the direct command SSPAAMOD <FRB|FRH>.

        Verified live on AVR-X1400H (2026-11-25): the receiver switches within
        2 s, pushes SSPAAMOD and keeps the current source (no on-screen menu).
        The receiver's confirmation is awaited (push, else GET_AVRSTS); only if it
        does not confirm, the on-screen-menu macro runs as a fallback. Front B then
        gets the A + B front speaker selection back (the change resets it), as the
        dashboard script did, and the speaker values are re-read.
        """

        spec = self._AMP_ASSIGN.get(mode)
        if spec is None:
            raise ValueError(f"Unsupported Amp Assign mode: {mode}")
        token, label, avrsts = spec
        use_menu = False
        async with self._amp_assign_menu_lock:
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
                    confirmed = await self._read_amp_assign_1256() == avrsts
                    if confirmed:
                        self._state.values["amp_assign"] = label
                        self._notifier.schedule()
                if not confirmed:
                    _LOGGER.warning("SSPAAMOD %s not confirmed; using the on-screen-menu fallback", token)
                    use_menu = True
        if use_menu:
            # The macro takes the same lock, so it runs after the block above.
            await self.async_set_amp_assign_from_menu(mode)
        if mode == "front_b":
            # The Amp Assign change resets the front speaker selection to A.
            await asyncio.sleep(1.0)
            await self._control.set_control("front_speaker", "A+B")
        self._sync._schedule_refresh("speaker_channel")
        self._sync._schedule_refresh("quick_select_audio")
        self._sync._schedule_refresh("front_speaker")

    async def async_set_amp_assign_from_menu(self, mode: str) -> None:
        """Local mod: set Amp Assign through the documented on-screen-menu commands.

        AVR-X1400H does not expose a discrete, readable Amp Assign command.
        Its menu cursor is preserved after closing the setup screen, therefore
        the macro explicitly returns to the Audio root entry before it exits.
        """

        directions = {"front_b": "MNCLT", "front_height": "MNCRT"}
        direction = directions.get(mode)
        if direction is None:
            raise ValueError(f"Unsupported Amp Assign mode: {mode}")

        menu: list[tuple[str, float]] = [
            # Restore the stable Audio entry using the AVR UI behavior the
            # receiver exposes: return through the hierarchy, open Option.
            ("MNMEN OFF", 0.35),
            ("MNOPT", 0.35),
            ("MNMEN OFF", 0.35),
            ("MNMEN ON", 0.35),
            ("MNCDN", 0.35),
            ("MNCDN", 0.35),
            ("MNCDN", 0.35),
            ("MNENT", 0.35),
            ("MNCDN", 0.35),
            ("MNENT", 0.35),
            ("MNENT", 0.35),
            # The AVR needs longer to enter the editable option list.
            ("MNENT", 0.5),
            (direction, 0.35),
            ("MNENT", 0.35),
            ("MNMEN OFF", 0.35),
        ]

        async with self._amp_assign_menu_lock:
            for command, delay in menu:
                await self._telnet.async_send(command)
                await asyncio.sleep(delay)
        # Amp Assign changes the active speaker layout and can alter channel
        # values. The AVR does not publish every dependent setting on its own.
        self._sync._schedule_refresh("speaker_channel")
        self._sync._schedule_refresh("quick_select_audio")

    async def async_select_sound_mode_group(self, code: str) -> None:
        await self._control.select_sound_mode_group(code)

    async def async_set_control(self, control_id: str, value: object) -> None:
        if control_id in {"subwoofer_level", "dialog_level"}:
            await self._set_level_with_enable(control_id, value)
            return
        await self._control.set_control(control_id, value)

    async def _set_level_with_enable(self, control_id: str, value: object) -> None:
        """Local mod: these levels have a separate enable state (PSSWL/PSDIL ON/OFF).

        Some receivers ignore a level change while that state is off.
        """

        spec = self._profile.control(control_id)
        command = commands.control(self._profile, control_id, value)
        if spec is None or command is None:
            return
        enable_prefix = spec.prefix
        if float(value) != 0:
            await self._telnet.async_send(f"{enable_prefix}ON")
            await self._telnet.async_send(command)
            return
        if control_id == "subwoofer_level":
            # The AVR can reject the zero level while adjustment is off.
            await self._telnet.async_send(f"{enable_prefix}ON")
        await self._telnet.async_send(command)
        await asyncio.sleep(0.25)
        await self._telnet.async_send(f"{enable_prefix}OFF")

    async def async_set_channel_trim(self, code: str, db: float) -> None:
        await self._control.set_channel_trim(code, db)

    async def async_set_channel_distance(self, code: str, meters: float) -> None:
        await self._control.set_channel_distance(code, meters)

    async def async_set_crossover(self, group: str, hertz: int) -> None:
        await self._control.set_crossover(group, hertz)

    async def async_set_crossover_mode(self, mode: str) -> None:
        await self._control.set_crossover_mode(mode)

    async def async_set_speaker_size(self, group: str, token: str) -> None:
        await self._control.set_speaker_size(group, token)
