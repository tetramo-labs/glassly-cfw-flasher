from .base import Device

ALL = frozenset({'darwin', 'linux', 'win32'})

DEVICES = {d.key: d for d in [
    Device(
        key='zero2',
        name='8BitDo Zero 2',
        supported='8BitDo Zero 2 with the YC3121 board, on official firmware v1.08.',
        update_mode='Unplug the USB cable and hold Start for three seconds to switch the controller off. '
                    'Hold L + R, then press Start. When the light starts blinking, release the buttons '
                    'and plug the controller into this computer with a USB data cable.',
        after_install='Unplug the cable and switch the controller on. Hold Select for three seconds until the light '
                      'blinks quickly, then add 8BitDo Zero 2 under Controllers in the Glassly App. '
                      'Pair it in Glassly, not in your phone\'s Bluetooth settings.',
        after_restore='Unplug the cable and switch the controller on. It is back on the official 8BitDo firmware.',
        module='glassly_flasher.devices.eightbitdo.zero2',
        elevate=frozenset({'linux'}),
        tested='Verified on one controller from macOS with the research flasher this app is ported from. '
               'Windows and Linux have not been tried on a controller yet.',
    ),
    Device(
        key='micro',
        name='8BitDo Micro',
        supported='8BitDo Micro on official firmware v1.05.',
        update_mode='Set the mode switch to D and plug the controller into this computer with a USB data cable. '
                    'The flasher switches it into update mode by itself.',
        after_install='Unplug the cable and set the switch to K. Switch the controller on, hold the pairing button until '
                      'the light blinks quickly, then add 8BitDo Micro under Controllers in the Glassly App.',
        after_restore='Unplug the cable. The controller is back on the official 8BitDo firmware.',
        module='glassly_flasher.devices.eightbitdo.micro',
        elevate=frozenset({'linux'}),
        tested='Verified on two controllers from macOS with the research flasher this app is ported from. '
               'Windows and Linux have not been tried on a controller yet.',
    ),
    Device(
        key='gamebrick',
        name='IINE Gamebrick Mini',
        supported='IINE Gamebrick Mini Retro Controller (L1161 / L1162).',
        update_mode='With the controller switched on and plugged into this computer, hold Pairing + B for about two '
                    'seconds until its light goes out, then release both.',
        after_install='Unplug the controller and switch it off and on again, then add IINE Gamebrick under Controllers in '
                      'the Glassly App. Any mode works (T, C or H).',
        after_restore='Unplug the controller and switch it off and on again. It is back on the official IINE firmware.',
        module='glassly_flasher.devices.gamebrick',
        elevate=ALL,
        modes=('install', 'restore', 'backup', 'restore-backup'),
        tested='Verified on two controllers from macOS with the research flasher this app is ported from. '
               'Windows and Linux have not been tried on a controller yet.',
    ),
    Device(
        key='ananke',
        name='IINE Ananke Gen 2',
        supported='IINE Mini Retro Ananke Gen 2 in Purple, Gray or Cream Yellow (L1254 / L1255 / L1277) on official '
                  'firmware V0900. Other colours use a different firmware and are refused.',
        update_mode='Switch the controller off and unplug it. Hold Capture + Home while plugging it into this computer, '
                    'then release.',
        after_install='Unplug the controller and switch it on. Hold Pair + D-pad Right until lights 2 and 3 flash, then '
                      'add IINE Ananke Gen 2 under Controllers in the Glassly App.',
        after_restore='Unplug the controller and switch it on. It is back on the official IINE firmware.',
        module='glassly_flasher.devices.ananke',
        elevate=ALL,
        tested='Verified on one controller from macOS with the research flasher this app is ported from. '
               'Windows and Linux have not been tried on a controller yet.',
    ),
    Device(
        key='memomind',
        name='MemoMind One',
        supported='MemoMind One smart glasses (model H001Y, hardware 2.1.2) on official firmware 1.0.3.127 '
                  'or on an earlier Glassly revision, over this computer\'s Bluetooth. There is no way '
                  'back to the official firmware yet, and the backup records the glasses\' identity only: their '
                  'firmware cannot be read out.',
        update_mode='Charge the glasses and switch them on. Disconnect them from the MemoMind app and switch off '
                    'Bluetooth on the phone they use. Then pair them with this computer: on a Mac in System Settings '
                    '> Bluetooth, on Windows in Settings > Bluetooth & devices > Add device > Bluetooth, on Linux in '
                    'your desktop\'s Bluetooth settings or with bluetoothctl. Pair only one pair of MemoMind glasses, '
                    'and keep them close to the computer until the flasher finishes.',
        after_install='Remove the glasses from this computer\'s Bluetooth settings, pair them in your phone\'s '
                      'Bluetooth settings, then add MemoMind One in the Glassly App.',
        after_restore='',
        after_backup='Nothing was written. The glasses\' identity was recorded in the backup folder.',
        module='glassly_flasher.devices.memomind',
        elevate=frozenset(),
        modes=('install', 'backup'),
        tested='Installed on one pair of glasses from macOS with the research tool this app is ported from. '
               'Windows and Linux Bluetooth have not been tried on glasses yet.',
    ),
]}
