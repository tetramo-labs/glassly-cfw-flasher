"""IINE Ananke Gen 2 (V0900 group): back up, install the Glassly firmware, or restore V0900.

Ported from glassly-ananke-cfw research/flash_ananke.py at 0cfd7ee364e8. Two matching full reads
are saved before any write; the plan is derived from them and only changed application sectors
are written, header sector last, each verified, then the whole flash is read back. No whole-flash
erase, fuse command or reset is ever sent.
"""
import json
import os
import sys

from ... import __version__
from ..base import Cancelled, FlashError
from ..jieli import Watchdog, loader_blob, wait_for_device
from ..jieli.link import AmbiguousTarget
from .image import applications, digest, plan, require
from .link import BR23_INQUIRY, SIZE, Br23BulkOnlyLink, Br23PassThroughLink
from .transport import INQUIRY, Transport, Writer, changed_sectors, read_full, readonly

SESSION_SECONDS = 600


def open_link():
    if sys.platform == 'darwin':
        return Br23BulkOnlyLink.load()
    if sys.platform == 'win32' or sys.platform.startswith('linux'):
        return Br23PassThroughLink()
    raise FlashError(f'Unsupported platform: {sys.platform}')


def save_json(path, value):
    with path.open('w') as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def run(mode, output, reporter, link=None):
    require(mode in ('install', 'restore', 'backup'), 'Invalid operation')
    stock = custom = None
    if mode != 'backup':
        stock, custom = applications()
        print('Official V0900 and Glassly applications verified.', flush=True)
    meta = dict(operation=mode, complete=False, backup_complete=False, flash_write_attempted=False,
                flash_modified=False, size=SIZE, platform=sys.platform, flasher_version=__version__)

    def record(values):
        meta.update(values)
        save_json(output/'metadata.json', meta)

    def journal(event):
        with (output/'commands.jsonl').open('a') as f:
            f.write(json.dumps(event)+'\n')
            f.flush()
            os.fsync(f.fileno())

    try:
        link = link or open_link()
    except OSError as e:
        raise FlashError(f'Cannot load the USB helper: {e}') from e
    record({})
    try:
        wait_for_device(link, reporter)
        with Watchdog(SESSION_SECONDS):
            record(dict(transport=link.transport, target=getattr(link, 'path', None) or '4c4a:2342'))
            reader = Transport(link)
            inquiry = reader.execute(INQUIRY, n=36)
            require(inquiry == BR23_INQUIRY, 'Unexpected identity; no loader uploaded')
            record(dict(inquiry=inquiry.hex()))
            print('BR23 / UBOOT1.00 identity verified.', flush=True)
            reporter.status('Loading the update helper into the controller…')
            record(reader.prepare(inquiry, loader_blob('br23loader.bin'), record))
            reporter.status('Reading the controller\'s flash (twice)…')
            before = read_full(reader, output/'before-pass1.bin', reporter.progress)
            second = read_full(reader, output/'before-pass2.bin', reporter.progress)
            require(before == second, 'Full backup reads disagree; no flash write')
            require(len(set(before)) >= 8, 'Implausible flash contents; retain incomplete capture')
            # Separate immutable backup metadata stays valid even if a later installation fails.
            backup = dict(complete=True, passes_match=True, flash_modified=False, size=len(before), sha256=digest(before),
                          files=['before-pass1.bin', 'before-pass2.bin'], inquiry=inquiry.hex())
            readonly(output/'backup.json', (json.dumps(backup, indent=2)+'\n').encode())
            record(dict(backup_complete=True, before_sha256=digest(before), passes_match=True))
            if mode == 'backup':
                record(dict(complete=True))
                print('BACKUP VERIFIED.', flush=True)
                return dict(mode=mode, sha256=digest(before), passes_match=True, flash_modified=False, flash_bytes=SIZE)
            desired = plan(before, stock, custom, mode)
            sectors = changed_sectors(before, desired)
            readonly(output/'desired.bin', desired)
            record(dict(desired_sha256=digest(desired), sectors=[hex(a) for a in sectors]))
            writer = Writer(link, reader, before, desired, journal)
            print(f'Preflight verified. Writing {len(sectors)} application sectors; header last.', flush=True)
            reporter.status(f'Writing {len(sectors)} sector(s)…')
            record(dict(flash_write_attempted=bool(sectors), flash_modified=bool(sectors)))
            for i, address in enumerate(sectors):
                writer.sector(address)
                record(dict(verified_sectors=i+1))
                print(f'Verified sector {i+1}/{len(sectors)}: {address:#x}', flush=True)
                reporter.progress(i+1, len(sectors))
            reporter.status('Reading the whole flash back to verify…')
            after = read_full(reader, output/'after.bin', reporter.progress)
            require(after == desired, 'Full flash readback mismatch; stop')
            record(dict(complete=True, full_readback_verified=True, after_sha256=digest(after)))
            print('FLASH VERIFIED.', flush=True)
            return dict(mode=mode, before_sha256=digest(before), after_sha256=digest(after),
                        sectors_written=len(sectors), full_readback_verified=True, flash_bytes=SIZE)
    except BaseException as exc:
        record(dict(error=str(exc), last_usb_command=link.last_command))
        if isinstance(exc, AmbiguousTarget):
            raise FlashError('More than one controller is in update mode. Connect only the one you want to flash.') from exc
        if isinstance(exc, (Cancelled, FlashError)) or not isinstance(exc, (RuntimeError, OSError, ValueError)):
            raise
        raise FlashError(str(exc)) from exc
    finally:
        link.close()
