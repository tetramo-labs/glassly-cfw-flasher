import subprocess
import sys
import time

from ..base import FlashError

VENDOR = 0x2dc8
BOOT_PRODUCT_ID = 0x3208
BOOT_NAME = '8BitDo Boot'
BOOT_DESCRIPTOR = bytes([5, 140, 9, 1, 161, 1, 9, 3, 21, 0, 38, 0, 255, 117, 8, 149, 64, 129, 2, 9, 4, 21, 0, 38, 0, 255,
                         117, 8, 149, 64, 145, 2, 192])
BUS_USB = 1
WAIT_SECONDS = 600


def hid_module():
    # The Linux wheel's `hid` module goes through libusb; hidraw keeps the kernel driver attached.
    if sys.platform.startswith('linux'):
        import hidraw
        return hidraw
    import hid
    return hid


def usb_devices(product_id):
    return [d for d in hid_module().enumerate(VENDOR, product_id) if d.get('bus_type', BUS_USB) == BUS_USB]


def open_device(info):
    device = hid_module().device()
    try:
        device.open_path(info['path'])
    except (OSError, IOError) as e:
        if sys.platform.startswith('linux'):
            raise FlashError('Cannot open the controller. The flasher needs administrator rights on Linux; '
                             'start it again and accept the password prompt.') from e
        raise FlashError(f'Cannot open the controller ({e}). Close any other app that is using it.') from e
    return device


def descriptor_matches(device, info, expected, usage_page, usage):
    # Windows rebuilds descriptors from parsed data, so exact bytes are only comparable elsewhere.
    if sys.platform == 'win32':
        return (info.get('usage_page'), info.get('usage')) == (usage_page, usage)
    try:
        return bytes(device.get_report_descriptor()) == expected
    except (OSError, IOError, ValueError):
        return False


class HidLink:
    def __init__(self, device):
        self.device = device

    def send(self, data):
        try:
            written = self.device.write(b'\x00' + data)
        except (OSError, IOError, ValueError) as e:
            raise FlashError(f'USB write failed: {e}') from e
        if written is None or written < len(data):
            raise FlashError('USB write failed')

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                return None
            try:
                data = self.device.read(64, remaining)
            except (OSError, IOError, ValueError) as e:
                raise FlashError(f'USB read failed: {e}') from e
            if data:
                return bytes(data)

    def drain(self):
        self.device.set_nonblocking(1)
        try:
            while self.device.read(64):
                pass
        except (OSError, IOError, ValueError):
            pass
        finally:
            self.device.set_nonblocking(0)

    def close(self):
        self.device.close()


def open_bootloader(info):
    if info.get('product_string') != BOOT_NAME:
        raise FlashError('The connected update-mode device is not an 8BitDo bootloader')
    device = open_device(info)
    if not descriptor_matches(device, info, BOOT_DESCRIPTOR, 0x8c, 0x01):
        device.close()
        raise FlashError('The connected 8BitDo bootloader has an unexpected USB descriptor; nothing was sent')
    return HidLink(device)


def connect(profile, reporter):
    """Waits for exactly one 8BitDo bootloader on USB and opens it."""
    reporter.status('Waiting for the controller in update mode')
    deadline = time.monotonic() + WAIT_SECONDS
    attempts, last_attempt, hinted = 0, 0.0, False
    while True:
        boots = usb_devices(BOOT_PRODUCT_ID)
        if len(boots) > 1:
            raise FlashError('More than one 8BitDo controller is in update mode. Connect only the one to flash.')
        if boots:
            time.sleep(0.5)
            return open_bootloader(boots[0])
        if profile.runtime_product is not None:
            runtime = usb_devices(profile.runtime_product)
            if len(runtime) > 1:
                raise FlashError(f'More than one {profile.name} is connected. Connect only the one to flash.')
            if runtime and profile.enter_update and time.monotonic() - last_attempt > 15:
                if attempts == 3:
                    raise FlashError('The controller did not switch into update mode. Check that the switch is on D, '
                                     'reconnect the cable and try again. Nothing was written.')
                reporter.status('Switching the controller into update mode')
                profile.enter_update(runtime[0], reporter)
                attempts, last_attempt = attempts + 1, time.monotonic()
            elif runtime and not profile.enter_update and not hinted:
                reporter.log(f'The {profile.name} is connected normally, not in update mode. '
                             'Unplug it and follow the update-mode steps.')
                hinted = True
        if time.monotonic() > deadline:
            raise FlashError('Timed out waiting for the controller in update mode. Nothing was written.')
        reporter.check_cancel()
        time.sleep(0.25)


def refuse_vendor_updater():
    if sys.platform == 'darwin':
        result = subprocess.run(['pgrep', '-f', '/Contents/MacOS/8BitDo Firmware Updater'], capture_output=True)
        running = result.returncode != 1
    elif sys.platform == 'win32':
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq 8BitDo Firmware Updater.exe', '/NH'],
                                capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        running = '8BitDo Firmware Updater' in result.stdout
    else:
        running = False
    if running:
        raise FlashError("Close 8BitDo's Firmware Updater first; it would compete for the controller.")
