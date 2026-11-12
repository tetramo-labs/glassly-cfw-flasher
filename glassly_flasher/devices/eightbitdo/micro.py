import hashlib
import sys

from . import Image, Profile, run as run_profile
from ..base import FlashError

RUNTIME_DESCRIPTOR_SHA256 = '2c5b161fb5dafa36f3337fcc8ced3b6f806d14cf4563817b6a1696b86988c264'
ENTER_UPDATE = bytes([0x06, 0x00, 0x50, 0x01]).ljust(64, b'\0')
ENTER_UPDATE_LEGACY = bytes([0x81, 0x05, 0x00, 0x51]).ljust(64, b'\0')


def enter_update(info, reporter):
    """Sends the vendor updater's two update-mode requests to a runtime Micro (ported from enter_update.c)."""
    from . import hidlink
    device = hidlink.open_device(info)
    try:
        if sys.platform == 'win32':
            matches = (info.get('usage_page'), info.get('usage')) == (1, 5)
        else:
            descriptor = bytes(device.get_report_descriptor())
            matches = len(descriptor) == 136 and hashlib.sha256(descriptor).hexdigest() == RUNTIME_DESCRIPTOR_SHA256
        if not matches:
            raise FlashError('The connected 2dc8:9020 device does not look like an 8BitDo Micro; nothing was sent')
        results = []
        for report in (ENTER_UPDATE, ENTER_UPDATE_LEGACY):
            try:
                results.append(device.write(report))
            except (OSError, IOError, ValueError) as e:
                results.append(-1)
                reporter.log(f'Update-mode request not accepted: {e}')
        reporter.log(f'Update-mode requests sent (results {results}).')
        if all(r is None or r < 0 for r in results):
            raise FlashError('The Micro rejected the update-mode request. Reconnect it with the switch on D.')
    finally:
        device.close()


PROFILE = Profile(
    name='8BitDo Micro',
    boot_product=0x9020,
    version=105,
    size=0x13000,
    dat_size=77852,
    header_offset=0,
    official='v1.05',
    runtime_product=0x9020,
    enter_update=enter_update,
    images={
        'install': Image('micro/glassly-micro-v1.05.dat', 'micro/glassly-micro-v1.05.plain.bin',
                         '72713359970e57dfd5697c807a7a5ba97ef521ed19fd87a3615332aefc5d3091',
                         '9e54738d54ae4dcb57a65f8306edefb660604c41c065f83948599a8132c2cf5e'),
        'restore': Image('micro/official-micro-v1.05.dat', 'micro/official-micro-v1.05.plain.bin',
                         '74f0a12709758cc13a358e44adaa1d6193a3532aac2685db07e913c8e3609ce6',
                         'e31c7f7656fb041ec6ede6603762d841783eb3d9b760321eaaa0c582aaa59233'),
    },
    baselines={
        'e31c7f7656fb041ec6ede6603762d841783eb3d9b760321eaaa0c582aaa59233': 'official v1.05',
        '7e758f2da3fdd4675fd67a9853ef70aed3e8b9c8dec8895c77338699e2e70541': 'factory (development unit)',
        '9e54738d54ae4dcb57a65f8306edefb660604c41c065f83948599a8132c2cf5e': 'Glassly',
        '7a16629b0ade32cad181bbb01181d2fe33d76d80083c1432ff8b75b67df706d4': 'Glassly (test01 name)',
        '5eab57a52b5df4df7c41b606243e0b40280b16c8af4277f71c58c26c348be1be': 'Glassly name probe',
        'fca4e5698166c69b67feabcce95e6674d7afcd72478f9acb25c3b9b71caf6e8e': 'Glassly name probe (test01 name)',
        '460a8ac2c12e1e8aa6d47a6a08079ad5f6abb1907910b9302ea3bf88b2be86b8': 'Glassly notification probe',
        '1c172f49d5cf74e9443277f4b904ad61fe7021c0c957acc2da2e5b91ed50a456': 'Glassly notification probe (test01 name)',
    },
)


def run(mode, output, reporter):
    return run_profile(PROFILE, mode, output, reporter)
