"""Tests for the local AVR-X1400H modifications (see LOCAL_MODS.md).

Run after every upstream merge: they catch a hook or an overlay key that an
upstream change silently broke.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import conftest  # noqa: F401  (loads the library as top-level ``avr``)
from avr.local_device import AMP_ASSIGN, LocalDenonAvrDevice, LocalTelnetParser
from avr.models import AvrState, Discovery
from avr.protocol.profile import load_profile

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "denon_avr"


def _parser():
    return LocalTelnetParser(load_profile(), Discovery())


# Profile overlay ---------------------------------------------------------------

def _fresh_profile():
    """A newly merged profile (the cached one is tuned by other tests)."""
    from avr.protocol.local_overlay import merge_local_overlay
    from avr.protocol.profile import ProtocolProfile

    raw = json.loads((ROOT / "avr" / "protocol" / "protocol_profile.json").read_text(encoding="utf-8"))
    return ProtocolProfile(merge_local_overlay(raw))


def test_overlay_is_merged_into_the_profile():
    profile = _fresh_profile()
    assert profile.sound_mode_wire_overrides["Dolby Surround"] == "DOLBY SURROUND"
    assert profile.sound_mode_wire_overrides["DTS Neural:X"] == "DTS SURROUND"
    assert profile.sound_mode_wire_overrides["Multi Ch Stereo"] == "MCH STEREO"  # upstream value kept
    assert profile.sound_mode_genre_commands["PUR"] == "AUTO"
    assert profile.sound_mode_genre_commands["MOV"] == "MOVIE"
    # The raw merged groups: conftest scales the profile's debounces down for speed.
    from avr.protocol.local_overlay import merge_local_overlay

    raw = json.loads((ROOT / "avr" / "protocol" / "protocol_profile.json").read_text(encoding="utf-8"))
    groups = merge_local_overlay(raw)["refresh"]["groups"]
    assert groups["speaker_channel"]["debounce"] == 2
    assert groups["speaker_channel"]["queries"][0] == "CV?"  # upstream list kept
    assert groups["quick_select_audio"]["queries"] == ["PSREFLEV ?"]
    assert groups["quick_select_input_mode"]["on"] == ["SI"]
    assert groups["channel_levels_signal"] == {**groups["channel_levels_signal"], "on": ["SSINFAISSIG"], "queries": ["CV?"]}
    assert groups["front_speaker"]["queries"] == ["PSFRONT?", "SSSPC ?", "CV?"]
    assert groups["front_speaker"]["on"] == ["SSPAAMOD"]  # upstream trigger kept


def test_only_the_local_device_gets_the_overlay():
    assert load_profile().sound_mode_genre_commands["PUR"] == "PURE DIRECT"  # upstream, for upstream's tests
    assert _device().profile.sound_mode_genre_commands["PUR"] == "AUTO"
    assert "channel_levels_signal" in _device().profile.refresh_groups


def test_upstream_profile_file_is_untouched_by_local_keys():
    raw = json.loads((ROOT / "avr" / "protocol" / "protocol_profile.json").read_text(encoding="utf-8"))
    assert "channel_levels_signal" not in raw["refresh"]["groups"]


# Parser --------------------------------------------------------------------------

@pytest.mark.parametrize(("line", "label"), [("SSPAAMOD FRB", "Front B"), ("SSPAAMODFRH", "Front Height")])
def test_amp_assign_push(line, label):
    state = AvrState()
    assert _parser().feed(line, state)
    assert state.values["amp_assign"] == label


def test_msquick0_keeps_a_recalled_preset():
    state, parser = AvrState(), _parser()
    parser.feed("MSQUICK2", state)
    assert state.values["quick_select"] == 2
    assert parser.feed("MSQUICK0", state) is False
    assert state.values["quick_select"] == 2


def test_source_change_clears_input_mode_and_quick_select():
    state, parser = AvrState(), _parser()
    parser.feed("SITV", state)
    state.values.update({"input_mode": "ARC", "quick_select": 1})
    parser.feed("SITV", state)  # same source: nothing cleared
    assert state.values["quick_select"] == 1
    parser.feed("SINET", state)
    assert state.zone("main").source == "NET"
    assert "input_mode" not in state.values and "quick_select" not in state.values


def test_other_lines_still_reach_the_upstream_parser():
    state = AvrState()
    assert _parser().feed("ZMON", state)
    assert state.values["main_power"] is True


# Device --------------------------------------------------------------------------

def _device():
    # The clients only keep the session; nothing connects in these tests.
    return LocalDenonAvrDevice(object(), "192.0.2.1")


def test_local_device_wires_the_local_parser():
    device = _device()
    assert isinstance(device._sync._parser, LocalTelnetParser)
    assert device._discovery_service._parser is device._sync._parser


def test_amp_assign_label_falls_back_to_the_speaker_setup():
    device = _device()
    assert device.amp_assign_label() is None
    device.state.speaker_sizes["FRH"] = "NON"
    assert device.amp_assign_label() == "Front B"
    device.state.speaker_sizes["FRH"] = "SMA"
    assert device.amp_assign_label() == "Front Height"
    device.state.values["amp_assign"] = "Front B"  # the push wins
    assert device.amp_assign_label() == "Front B"


def test_amp_assign_modes():
    assert AMP_ASSIGN["front_b"][0] == "FRB" and AMP_ASSIGN["front_height"][0] == "FRH"


# Hooks ---------------------------------------------------------------------------

@pytest.mark.parametrize(("file", "needle"), [
    ("coordinator.py", "LocalDenonAvrDevice(session, host)"),
    ("catalog.py", "adjust_catalog(device, known_keys, entries)"),
    ("button.py", "button_factories(coordinator)"),
    ("sensor.py", "sensor_factories(coordinator)"),
    ("select.py", "select_factories(coordinator, factories)"),
    ("media_player.py", "media_player_factories(coordinator)"),
    ("avr/device.py", "_profile_loader"),
    ("avr/__init__.py", "LocalDenonAvrDevice"),
])
def test_local_hooks_are_present(file, needle):
    assert needle in (ROOT / file).read_text(encoding="utf-8"), f"LOCAL HOOK missing in {file}"


# Catalogue -----------------------------------------------------------------------

def test_catalogue_adds_the_amp_assign_entities_only_for_the_x1400h():
    from types import SimpleNamespace

    local_catalog = conftest.load_ha_module("local_catalog")

    def device(model):
        return SimpleNamespace(discovery=SimpleNamespace(device=SimpleNamespace(model_name=model)))

    keys = {e.key for e in local_catalog.adjust_catalog(device("AVR-X1400H"), set(), [])}
    assert keys == {"select_amp_assign", "button_amp_assign_front_b", "button_amp_assign_front_height", "sensor_amp_assign"}
    assert local_catalog.adjust_catalog(device("AVC-X3900H"), set(), []) == []
