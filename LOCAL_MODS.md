# Local modifications (AVR-X1400H)

This fork is upstream [LaserGuruGuy/denon_avr](https://github.com/LaserGuruGuy/denon_avr)
plus local changes for an AVR-X1400H. The local code is kept **separate** from the upstream
code so that new upstream releases merge without (or with trivial) conflicts.

## Where the local code lives

| File | What |
|---|---|
| `custom_components/denon_avr/avr/local_device.py` | `LocalDenonAvrDevice` (subclass of the facade) and `LocalTelnetParser` |
| `custom_components/denon_avr/local_entities.py` | Amp Assign select / buttons / sensor, entity tweaks |
| `custom_components/denon_avr/local_catalog.py` | Catalogue additions (HA-free, like `catalog.py`) |
| `custom_components/denon_avr/avr/protocol/protocol_profile.local.json` | Overlay deep-merged over `protocol_profile.json` at load time |
| `custom_components/denon_avr/avr/protocol/local_overlay.py` | The overlay loader (`load_local_profile`, used only by `LocalDenonAvrDevice`) |
| `tests/test_local_mods.py` | Tests for all of the above, incl. a check that every hook is present |

Upstream files only carry one or two lines marked `# LOCAL HOOK` (search for it):
`avr/__init__.py`, `avr/device.py` (profile loader), `coordinator.py`, `catalog.py`, `button.py`,
`sensor.py`, `select.py`, `media_player.py`.

## What the local changes do

- **Amp Assign** (Front B / Front Height) with the direct command `SSPAAMOD FRB|FRH`
  (verified live: switches in < 2 s, no on-screen menu, the source stays). Confirmed by the
  `SSPAAMOD` push, else by re-reading the speaker setup. Right after power on the receiver
  ignores the command for some seconds, so it is retried (3 attempts, ~6.5 s each); the
  on-screen-menu macro is only the fallback after all of them. Front B puts the front
  speakers back on **A + B**.
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

## Installing and updating in Home Assistant

Home Assistant installs this fork through the **Marketplace** as a custom repository
(`emanueldan/denon_avr`, category Integration), from its GitHub releases
(`vX.Y.Z-x1400h.N`). Upstream `LaserGuruGuy/denon_avr` is not installed there, so a
Marketplace update can never replace the local changes with upstream's code. To ship a
change: push to `main`, let the tests pass, publish a release; Home Assistant then offers
it as a normal update.

Home Assistant watches upstream once a week (REST sensors on the latest upstream release
and `main` commit, compared with the upstream version this fork is based on) and shows a
notification when there is something new to take.

Install Home Assistant's copy **from this fork** (copy `custom_components/denon_avr`, or add
this repository to HACS as a custom repository): installing upstream's release directly would
overwrite the local changes.

The overlay applies only to `LocalDenonAvrDevice` (the device Home Assistant uses):
upstream's own tests and golden files keep running against upstream's profile and stay
untouched, so they never conflict on a merge.
