# Glassly CFW Flasher

One desktop app that installs the Glassly button firmware on four game controllers, so they work as
controllers in the Glassly App on iOS and Android. It can also put the official firmware back. The same
app installs the Glassly firmware on MemoMind One smart glasses, over Bluetooth instead of USB; see
[MemoMind One glasses](#memomind-one-glasses).

The firmware is built into the app. Nothing is downloaded while you flash, and every image is checked
against a pinned SHA256 before it goes near a controller.

| Controller | Accepted starting firmware | Back up only |
| --- | --- | --- |
| 8BitDo Zero 2 (YC3121 board) | Official v1.08 | Yes |
| 8BitDo Micro | Official v1.05 | Yes |
| IINE Gamebrick Mini (L1161 / L1162) | Factory firmware or the official IINE update, 2025 production run only (flash part `cd7113`, two-slot layout). Newer units (flash part `cd7013`, IINE's current layout) are recognised but only backed up; see below | Yes |
| IINE Mini Retro Ananke Gen 2, Purple / Gray / Cream Yellow (L1254 / L1255 / L1277) | Official V0900 | Yes |
| MemoMind One glasses (H001Y, hardware 2.1.2) | Official 1.0.3.127, or a Glassly revision it has installed over before. Restore sends the official 1.0.3.127 image through the same updater; this app has not yet run that on glasses | Identity only; the firmware cannot be read out |

This is experimental firmware. Each controller has been flashed and verified from macOS with the research
flasher this app is ported from. The app itself, and every Windows and Linux path, has not yet been run
against a real controller. A failed flash can leave a controller unusable.

## Download

Get the file for your computer from the [latest release](https://github.com/tetramo-labs/glassly-cfw-flasher/releases/latest):

| Computer | File |
| --- | --- |
| Mac with Apple silicon (M1 or later) | `Glassly-CFW-Flasher-macOS-AppleSilicon.dmg` |
| Mac with an Intel processor | `Glassly-CFW-Flasher-macOS-Intel.dmg` |
| Windows 10 or 11, 64-bit | `Glassly-CFW-Flasher-Windows-x64.exe` |
| Linux, x86_64 | `Glassly-CFW-Flasher-Linux-x86_64` |

`SHA256SUMS.txt` in the release lists the checksum of each file.

### macOS

Open the `.dmg` and drag **Glassly CFW Flasher** into Applications. The app is not notarized, so macOS
blocks the first launch: open **System Settings > Privacy & Security**, find the message about Glassly CFW
Flasher and click **Open Anyway**. For the IINE controllers, macOS asks for your password when you press
Start, because capturing a USB device needs administrator rights. The 8BitDo controllers need no password.

### Windows

Run the `.exe`. If SmartScreen says it protected your PC, click **More info > Run anyway**. The app asks for
administrator rights when it starts; the IINE controllers are flashed through raw disk access, which Windows
only allows to administrators. No driver install is needed for any controller.

### Linux

```sh
chmod +x Glassly-CFW-Flasher-Linux-x86_64
./Glassly-CFW-Flasher-Linux-x86_64
```

Pressing Start asks for your password through polkit (`pkexec`), which every mainstream desktop has. Without
a desktop, use the [command line](#command-line) with `sudo`.

## Flash a controller

1. Connect only the controller you want to flash, with a USB **data** cable.
2. In the app, choose the controller and what to do: install the Glassly firmware, put the official firmware
   back, or (IINE controllers) just take a backup. The Gamebrick also offers putting back its own earlier backup:
   pick the `flash-pass1.bin` from that session's folder.
3. Tick the box acknowledging the risk and press **Start**.
4. Put the controller in update mode. The app shows the steps and waits up to ten minutes:
   - **8BitDo Zero 2**: unplug, hold Start for three seconds to switch off, hold **L + R** and press
     **Start**. When the light blinks, release and plug in.
   - **8BitDo Micro**: set the switch to **D** and plug in. The app switches it to update mode itself.
   - **IINE Gamebrick Mini**: switched on and plugged in, hold **Pairing + B** for about two seconds until
     the light goes out, then release.
   - **IINE Ananke Gen 2**: switch off and unplug, hold **Capture + Home** while plugging in, then release.
5. Keep the cable connected until the app says the firmware is installed and verified.
6. Pair it inside the Glassly App, not in your phone's Bluetooth settings:
   - **Zero 2**: switch on, hold **Select** for three seconds, then **Controllers > 8BitDo Zero 2**.
   - **Micro**: set the switch to **K**, switch on, hold the pairing button, then **Controllers > 8BitDo Micro**.
   - **Gamebrick Mini**: power-cycle it, then **Controllers > IINE Gamebrick**. Any mode (T, C or H) works.
   - **Ananke Gen 2**: switch on, hold **Pair + D-pad Right** until lights 2 and 3 flash, then
     **Controllers > IINE Ananke Gen 2**.

Cancel works while the app is still waiting for the controller. Once it starts writing, it finishes; closing
the window mid-flash is blocked.

### What the app refuses, and why

On 2026-11-13 a tester's Gamebrick of a kind the firmware had never been flashed on (a newer
production run with a different flash layout and flash part) passed every identity check, took a
fully verified flash, and then never came up again. Since then the app only installs or restores on
kinds of unit the firmware has been flashed and seen booting on. Any other unit can be backed up and
have its own backup put back, nothing more; the message names what the controller reported so the
kind can be reviewed. Send that backup folder rather than looking for a way around the refusal: the
first flash on a new kind of unit is done on a developer-owned controller, never on yours.

### What the app checks

- It reads the controller first and saves a full backup before anything is erased.
- It refuses to erase a controller whose current firmware is not one it recognises. You keep the backup.
- It writes only the application area. IINE controllers keep their pairing data, settings, calibration and
  keys; the 8BitDo bootloader is never touched.
- It verifies every written byte before committing (8BitDo) or reads the whole flash back (IINE).

Backups and a log of every session are kept in:

- macOS: `~/Library/Application Support/Glassly CFW Flasher/backups`
- Windows: `%LOCALAPPDATA%\Glassly CFW Flasher\backups`
- Linux: `~/.local/share/glassly-cfw-flasher/backups`

The **Open backup folder** button opens the current session's folder. IINE backups contain the controller's
keys and pairing data; keep them private.

### If it stops

- **"firmware the flasher does not recognise"** on an 8BitDo: nothing was erased. Install the official
  firmware (Zero 2 v1.08, Micro v1.05) with 8BitDo's Firmware Updater, close it, and run the flasher again.
  Some factory units need this once.
- An IINE controller refused before writing (unrecognised firmware, partition layout or configuration):
  nothing was written. Keep the backup folder and open an issue.
- Anything that stops after writing has begun: leave the controller plugged in, keep the backup folder and
  open an issue with the session's `operation.log`.

## MemoMind One glasses

The flasher installs Glassly firmware revision 25 (`GLASSLYCFW/25`), the build the Glassly App connects to,
including from an iPhone over Bluetooth LE. It goes through the glasses' own Bluetooth Classic updater, so it needs a computer with
Bluetooth and no cable.

### Pair the glasses with the computer first

Charge the glasses and switch them on. Disconnect them from the MemoMind app and switch Bluetooth off on the phone
they use, then pair them with this computer. They appear as **MemoMind One**.

- **macOS**: System Settings > Bluetooth, then **Connect** next to MemoMind One. The first time you press Start,
  macOS asks whether Glassly CFW Flasher may use Bluetooth; allow it.
- **Windows**: Settings > Bluetooth & devices > Add device > Bluetooth, then MemoMind One.
- **Linux**: your desktop's Bluetooth settings, or `bluetoothctl` with `scan on`, `pair <address>` and
  `trust <address>`. The flasher needs BlueZ (`bluetoothctl`); `sdptool` is used when installed. No password is
  asked for: Bluetooth needs no administrator rights.

Pair only one pair of MemoMind glasses with the computer. The flasher refuses if it finds none or more than one, and
waits up to ten minutes for them to be paired.

### What Install does

1. Reads the glasses' identity and installed firmware over one connection and saves them to the session folder
   (`device-info.json`, `capabilities.json`). It writes only to glasses that report model H001Y, hardware 2.1.2,
   front 1.3 and official firmware 1.0.3.127, with the MAC address they are paired under: the research flasher's
   check, minus 1.0.2.151, which no Glassly firmware has ever been flashed from. Glasses already on Glassly firmware are accepted only on a revision the
   firmware has been installed over before (1, 3 to 6, 10, 11, 14, 16 to 20 and 22); revision 25 is left alone.
2. Eight seconds later, on a second connection, it checks the identity again, tells the glasses an update is coming,
   opens their update service and sends the image in the blocks the glasses ask for (about 550 blocks; a few
   minutes), then asks them to restart. Nothing is resent: any unexpected reply stops the transfer.
3. It waits up to five minutes for the glasses to come back as `GLASSLYCFW/25`. Wake them if their display stays
   dark.

Then remove the glasses from the computer's Bluetooth settings, pair them in your phone's Bluetooth settings and add
MemoMind One in the Glassly App.

If the transfer stops partway (out of range, flat battery, the computer goes to sleep), the glasses keep running the
firmware they had: they only switch after receiving and checking the whole image. On the developer's glasses this
happened twice, once after 16 KiB and once after 1.19 MB; the second time the glasses also kept the partial
transfer, and a later run continued from where it stopped. Run Install again; it continues the same way when the
glasses still hold the partial transfer, and starts over otherwise. What happens when the glasses lose power after
the last block, while they check and switch to the new firmware, has not been seen.

**Back up only** reads the identity and installed firmware and writes nothing. The glasses' firmware has no command
that reads their flash back, so no firmware image is saved. **Put the official firmware back** sends the official
1.0.3.127 image, the vendor's own release that the MemoMind app installed on the development glasses, through the
same updater, from revision 25 or any revision Install accepts, and waits for the glasses to answer as official
firmware again. This app has not yet run that path on real glasses; the first restore is done on the developer's pair.

### What has been tested

The install sequence was run from macOS by the research tool this code is ported from: revision 25 was installed and
booted on one pair of glasses on 2026-11-11, after many earlier revisions on the same glasses. This app's own
Bluetooth code has not yet been run against glasses on any computer; its macOS helper uses the same IOBluetooth calls
as the research tool, and its tests replay the recorded revision 25 install, an interrupted and continued install and
the MemoMind app's own update, byte for byte. Windows and Linux Bluetooth have not been tried on glasses.

## Command line

The same app runs without a window. On Linux:

```sh
sudo ./Glassly-CFW-Flasher-Linux-x86_64 --cli <zero2|micro|gamebrick|ananke> <install|restore|backup> --i-accept-the-risk
```

The MemoMind One glasses (`memomind`, `install`, `restore` or `backup`) need no `sudo` on any system.

On macOS the executable is `/Applications/Glassly CFW Flasher.app/Contents/MacOS/Glassly CFW Flasher`; it
needs `sudo` only for the IINE controllers. The Windows build has no console, so on Windows run the command
line from a source checkout (`python -m glassly_flasher --cli ...` in an Administrator terminal).

## Development

Python 3.13:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-build.txt
.venv/bin/python -m glassly_flasher                     # run the app from source
.venv/bin/python -m unittest discover -s tests -v       # offline tests, no hardware
.venv/bin/python scripts/build.py                       # build this OS's download into dist/release/
```

Tests use fake devices. The 8BitDo tests also replay the USB transcripts of the real hardware flashes and
require byte-identical packets; those replays skip when the private captures are not on disk.

To release, bump `__version__` in `glassly_flasher/__init__.py`, then push a matching tag:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

The Release workflow builds all four downloads on GitHub's macOS, Windows and Linux runners, smoke-tests
each one, and publishes them with `SHA256SUMS.txt`.

## Where the firmware comes from

Each `firmware/<controller>/manifest.json` records the size, SHA256 and source commit of every bundled file.
The firmware is developed in:

- [glassly-8bitdo-zero2-cfw](https://github.com/tetramo-labs/glassly-8bitdo-zero2-cfw)
- [glassly-8bitdo-micro](https://github.com/tetramo-labs/glassly-8bitdo-micro)
- [glassly-gamebrick-mini-cfw](https://github.com/tetramo-labs/glassly-gamebrick-mini-cfw)
- [glassly-ananke-cfw](https://github.com/tetramo-labs/glassly-ananke-cfw)
- [glassly-memomind-one-cfw](https://github.com/tetramo-labs/glassly-memomind-one-cfw)

The IINE USB code uses parts of kagaimiq's MIT-licensed [jl-misctools](https://github.com/kagaimiq/jl-misctools)
and [jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool), vendored under `third_party/` with their
licenses. USB HID access uses [hidapi](https://github.com/trezor/cython-hidapi).
