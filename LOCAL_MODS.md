# Local modifications (AVR-X1400H)

This fork is upstream [LaserGuruGuy/denon_avr](https://github.com/LaserGuruGuy/denon_avr)
plus local changes for an AVR-X1400H. The local code is kept **separate** from the upstream
code so that new upstream releases merge without (or with trivial) conflicts.

## Where the local code lives

| File | What |
|---|---|
| `custom_components/denon_avr/avr/local_device.py` | `LocalDenonAvrDevice` (subclass of the facade) and `LocalTelnetParser` |
| `custom_components/denon_avr/local_entities.py` | Amp Assign select / buttons / sensor, entity tweaks, catalogue additions |
| `custom_components/denon_avr/avr/protocol/protocol_profile.local.json` | Overlay deep-merged over `protocol_profile.json` at load time |
| `custom_components/denon_avr/avr/protocol/local_overlay.py` | The overlay loader |
| `tests/test_local_mods.py` | Tests for all of the above, incl. a check that every hook is present |

Upstream files only carry one or two lines marked `# LOCAL HOOK` (search for it):
`avr/__init__.py`, `avr/protocol/profile.py`, `coordinator.py`, `catalog.py`, `button.py`,
`sensor.py`, `select.py`, `media_player.py`.

## What the local changes do

- **Amp Assign** (Front B / Front Height) with the direct command `SSPAAMOD FRB|FRH`
  (verified live: switches in < 2 s, no on-screen menu, the source stays). Confirmed by the
  `SSPAAMOD` push, else by re-reading the speaker setup; the on-screen-menu macro is only a
  fallback. Front B puts the front speakers back on **A + B**.
  Entities: `select.avr_x1400h_speakers_amp_assign`, `button.avr_x1400h_speakers_amp_assign_front_b`,
  `button.avr_x1400h_speakers_amp_assign_front_height`, `sensor.avr_x1400h_speakers_amp_assign`.
- **Dolby Surround**: the AVR-X1400H ignores `MSDOLBY SURROUND` as a setter; it is reached by
  repeating `MSMOVIE` until the receiver confirms it. Pure group = `MSAUTO`; DTS wire overrides.
- **Quick Select**: published immediately (the receiver does not echo `MSQUICK`), `MSQUICK0`
  ignored after a recall, cleared on a real source change, speaker values re-read after a recall;
  without a known preset the select derives one from the input mode.
- **Subwoofer / dialog level**: `PSSWL ON` / `PSDIL ON` before a level is set.
- **Input mode** select limited to ARC/eARC on the TV source when that is active.
- **Channel levels** (`CV?`) re-read on an audio signal change and after an Amp Assign change
  (the receiver reports them only while audio plays).
- **Fixed volume scale**: `volume_level = raw / 100` like the core `denonavr` integration
  (0.30 = 30 = -50 dB), independent of the `MVMAX` the receiver reports.

## Taking a new upstream release

```bash
git remote add upstream https://github.com/LaserGuruGuy/denon_avr.git   # once
git fetch upstream --tags
git checkout main
git merge vX.Y.Z            # the new upstream tag
# conflicts, if any, are in the LOCAL HOOK lines only: keep upstream's code + re-add the hook
python -m pytest tests      # test_local_mods.py must pass (hooks, overlay, parser, device)
git push origin main
git tag vX.Y.Z-x1400h.1 && git push origin vX.Y.Z-x1400h.1   # HACS installs this release
```

If upstream renames something the local code relies on (`_sync._parser`,
`_discovery_service._parser`, `_dispatch`, `refresh.groups`, factory keys), `test_local_mods.py`
and `LocalDenonAvrDevice.__init__` fail loudly instead of silently dropping a change.

Home Assistant installs this fork through HACS (custom repository), so an HACS update never
overwrites the local changes with upstream's code.
