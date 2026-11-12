# Working instructions

This is the end-user flasher for the Glassly controller firmware. The firmware itself is developed
in the per-controller repositories; this repo bundles their hardware-verified images and ports
their flashers. Read README.md first.

## Rules that exist because of the 2026-11-13 Gamebrick incident

A tester's Gamebrick of a previously unseen kind (new flash layout, new flash part, new factory
build) was accepted by identity checks, flashed with a fully verified write, and then never came
up again. The write was correct; the application did not run on that hardware. Nothing below may
be relaxed to get past a refusal.

- **Writes are gated on a hardware-verified allowlist, not on structural validity.** Each device
  module keeps an explicit set of unit kinds (for the Gamebrick: `(flash layout, flash part)` in
  `devices/gamebrick/layout.py::HARDWARE_VERIFIED`; for the 8BitDo controllers: the exact
  installed baseline hashes; for the Ananke: the exact bootloader, configuration, layout and
  application hashes) on which the shipped images have been flashed and seen booting. Any other
  unit gets backup and "restore own backup" only, with a message asking for the backup folder.
- **Accepting a new kind and writing to it are never one change.** The sequence is: the unit is
  backed up (backup-only modes exist for every controller); the dump is decoded and reviewed
  offline; the first flash of that kind happens on a developer-owned controller with a proven way
  back; only then is the kind added to the allowlist, in its own commit, citing the evidence.
- **Only hardware-verified images ship.** An image that has only passed emulation or offline
  checks is not bundled for install, even if it looks like a small change; it is flashed on the
  developer's unit first. The Zero 2 steady-LED build is such an image and is not bundled.
- **Every write mode keeps the way back.** Backups are taken before any erase and the app can
  restore a controller's own earlier backup; a session's `flash-pass1.bin` is the recovery image
  of record. Never add a mode that writes without a backup.
- **Documented exception: MemoMind One.** Its stock firmware has no command to read its flash, and
  the official image has never been flashed back through its updater on hardware, so Install has no
  way back and Restore is not offered. The update itself is the vendor's own staged, CRC-checked OTA
  (an interrupted transfer leaves the old firmware booting), and the identity gate admits only the
  exact model, hardware, firmware and Glassly revisions the research captures show were flashed from.
  Do not widen it, and do not add Restore until the official image has gone back on a developer's unit.
- **Refusals name what was seen.** Identity refusals print the values the controller reported
  (flash part, layout, application build) so a new kind can be reviewed from the message alone.

Do not commit AI attribution. Keep `.gitattributes` (`firmware/** -text`): Windows checkouts
once rewrote a bundled JSON and broke its pinned hash.
