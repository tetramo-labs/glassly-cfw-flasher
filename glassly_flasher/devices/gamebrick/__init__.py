"""IINE Gamebrick Mini: install the Glassly firmware, restore the official one, or back up.

Reads the whole flash twice and saves it, derives the image from that read, writes only the
application area and verifies the entire flash afterwards. Runtime storage and key/MAC are
never written. Ported from glassly-gamebrick-mini-cfw research/flash_gamebrick.py at 9d51cdf93e5b.
"""
import os
import sys

from ..base import Cancelled, FlashError
from ..jieli import Watchdog, wait_for_device
from ..jieli.link import AmbiguousTarget, BulkOnlyLink, PassThroughLink
from .image import SIZE, classify, digest, glassly_application, official_application, plan, require
from .transport import Transport, program_with_plan, read_flash

VENDOR, PRODUCT = 0x4c4a, 0x3442
SESSION_SECONDS = 2100


def is_bd19_uboot(inquiry):
    return (inquiry[8:16].decode('ascii', 'replace').strip() == 'BD19' and
            inquiry[16:32].decode('ascii', 'replace').strip() == 'UBOOT1.00')


def open_link():
    if sys.platform == 'darwin':
        return BulkOnlyLink.load(VENDOR, PRODUCT)
    if sys.platform == 'win32' or sys.platform.startswith('linux'):
        return PassThroughLink(is_bd19_uboot)
    raise FlashError(f'Unsupported platform: {sys.platform}')


def run(mode, output, reporter, link=None, backup=None):
    require(mode in ('install', 'restore', 'backup', 'restore-backup'), 'Invalid operation')
    oem = custom = None
    if mode == 'restore-backup':
        if backup is None or len(backup) != SIZE or classify(backup) == 'unknown':
            raise FlashError('The chosen file is not a complete read of a known Gamebrick image')
        print(f'Backup carries the {classify(backup)} application.', flush=True)
    if mode != 'backup':
        # Verify both bundled applications before touching the controller.
        oem = official_application()
        custom = glassly_application()
        print('Official and Glassly applications verified.', flush=True)
    try:
        link = link or open_link()
    except OSError as e:
        raise FlashError(f'Cannot load the USB helper: {e}') from e

    def save(name, data):
        with (output/name).open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    try:
        wait_for_device(link, reporter)
        with Watchdog(SESSION_SECONDS):
            device = Transport(link, [])
            reporter.status('Loading the update helper into the controller…')
            device.initialize()
            if mode == 'backup':
                reporter.status('Reading the controller\'s flash (twice)…')
                first = read_flash(device, reporter.progress)
                save('flash-pass1.bin', first)
                second = read_flash(device, reporter.progress)
                save('flash-pass2.bin', second)
                require(first == second, 'Backups differ; do not flash')
                report = {'sha256': digest(first), 'state': classify(first), 'passes_match': True, 'flash_modified': False}
                print('BACKUP COMPLETE:', report['sha256'], f'({report["state"]} firmware)', flush=True)
            else:
                planned = {}

                def source_and_target(current):
                    planned.update(plan(current, oem, mode, custom, backup))
                    print(f'Controller firmware: {planned["state"]}; {len(planned["changed_sectors"])} sector(s) to write.', flush=True)
                    save('stock.bin', planned['stock'])
                    save('target.bin', planned['target'])
                    reporter.status(f'Writing {len(planned["changed_sectors"])} sector(s)…')
                    return current, planned['target']

                reporter.status('Reading the controller\'s flash (twice) before writing…')
                report = program_with_plan(device, source_and_target, save, reporter.progress)
                report.update({k: v for k, v in planned.items() if k not in ('target', 'stock')})
                report['changed_sectors'] = [hex(a) for a in report['changed_sectors']]
                print('FLASH VERIFIED:', report['sha256'], flush=True)
            report.update(mode=mode, flash_bytes=SIZE)
            return report
    except Cancelled:
        raise
    except AmbiguousTarget as e:
        raise FlashError('More than one controller is in update mode. Connect only the one you want to flash.') from e
    except FlashError:
        raise
    except (RuntimeError, OSError, ValueError) as e:
        raise FlashError(str(e)) from e
    finally:
        link.close()
