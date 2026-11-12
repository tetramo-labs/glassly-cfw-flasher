"""8BitDo USB bootloader (2dc8:3208) flasher shared by the Zero 2 and Micro.

Ported from the hardware-verified flash_zero2.c and flash_micro.c in the firmware repositories.
The packet sequence is unchanged: identify, back up application and metadata, refuse unknown
baselines before erasing, write, verify every plaintext byte, then commit the version and reset.
"""
from dataclasses import dataclass
import hashlib
import os
import struct
from typing import Callable, Optional

from ... import paths
from ..base import FlashError

BASE = 0x01018000
VERSION_ADDRESS = 0x01017040
META_BASE = 0x01017000
META_SIZE = 0x1000
WRITE_CHUNK = 46
READ_CHUNK = 32


@dataclass(frozen=True)
class Image:
    dat: str
    plain: str
    dat_sha256: str
    plain_sha256: str


@dataclass(frozen=True)
class Profile:
    name: str
    boot_product: int
    version: int
    size: int
    dat_size: int
    header_offset: int
    images: dict
    baselines: dict
    official: str
    enter_update: Optional[Callable] = None
    runtime_product: Optional[int] = None


def crc16(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return crc


def u16(data, offset):
    return struct.unpack_from('<H', data, offset)[0]


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def packet(cmd, address, total, n, data=None):
    if n > WRITE_CHUNK or (data is not None and len(data) != n):
        raise ValueError('Invalid bootloader packet')
    out = bytearray(64)
    out[0] = 5
    struct.pack_into('<H', out, 1, cmd)
    struct.pack_into('<H', out, 5, n)
    struct.pack_into('<I', out, 9, total)
    struct.pack_into('<I', out, 13, address)
    if data is not None:
        out[17:17 + n] = data
    struct.pack_into('<H', out, 7, crc16(out[17:17 + n]))
    return bytes(out)


def is_ack(reply, cmd):
    return (reply is not None and 18 <= len(reply) <= 64 and reply[0] == 0 and reply[1] == 5
            and u16(reply, 2) == 0 and u16(reply, 4) == cmd)


def is_read_reply(reply, cmd, address, want):
    if not is_ack(reply, cmd):
        return False
    length = u16(reply, 6)
    if length < want or length > WRITE_CHUNK or len(reply) < 18 + length or crc16(reply[18:18 + length]) != u16(reply, 8):
        return False
    return cmd != 5 or (length == want and u32(reply, 10) == want and u32(reply, 14) == address)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_image(profile, image):
    dat = paths.firmware(image.dat)
    plain = paths.firmware(image.plain)
    if sha256(dat) != image.dat_sha256 or sha256(plain) != image.plain_sha256:
        raise FlashError('Refusing an unreviewed firmware image')
    check_image(profile, dat, plain)
    return dat, plain


def check_image(profile, dat, plain):
    header = dat[profile.header_offset:profile.header_offset + 28]
    if (len(dat) != profile.dat_size or len(plain) != profile.size or u32(header, 0) != profile.version
            or u32(header, 4) != BASE or u32(header, 8) != profile.size or u32(header, 12) != profile.boot_product):
        raise FlashError('Firmware image header does not match this controller')


class Bootloader:
    def __init__(self, link, transcript=None):
        self.link = link
        self.transcript = transcript

    def dump(self, label, data):
        if self.transcript:
            self.transcript.write(f'{label}={bytes(data).hex()}\n')
            self.transcript.flush()

    def exchange(self, cmd, address, total, n, data=None, wait=True):
        out = packet(cmd, address, total, n, data)
        self.link.drain()
        self.dump('tx', out)
        self.link.send(out)
        if not wait:
            return None
        reply = self.link.receive(10.0 if cmd == 4 else 3.0)
        if reply is not None:
            self.dump('rx', reply)
        if not is_ack(reply, cmd):
            raise FlashError(f'The controller did not acknowledge command {cmd} at {address:#010x}')
        return reply

    def read(self, address, count, progress=None):
        out = bytearray()
        while len(out) < count:
            n = min(READ_CHUNK, count - len(out))
            at = address + len(out)
            reply = self.exchange(5, at, n, n)
            if not is_read_reply(reply, 5, at, n):
                raise FlashError(f'Invalid read reply at {at:#010x}')
            out += reply[18:18 + n]
            if progress:
                progress(len(out), count)
        return bytes(out)

    def boot_product(self):
        reply = self.exchange(8, 0, 2, 2)
        if not is_read_reply(reply, 8, 0, 2):
            raise FlashError('Invalid bootloader identity reply')
        return u16(reply, 18)


def save(folder, name, data):
    with (folder / name).open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def session(profile, mode, link, output, reporter, image):
    with (output / 'transport.log').open('x') as transcript:
        boot = Bootloader(link, transcript)
        product = boot.boot_product()
        if product != profile.boot_product:
            raise FlashError(f'This is not an {profile.name} (bootloader reports product {product:#06x}). '
                             'Choose the matching controller in the flasher.')
        reporter.log(f'{profile.name} bootloader identified (product {product:#06x}).')
        reporter.status('Backing up the controller')
        before = boot.read(BASE, profile.size, reporter.progress)
        save(output, 'before-application.bin', before)
        metadata = boot.read(META_BASE, META_SIZE)
        save(output, 'before-metadata.bin', metadata)
        baseline = profile.baselines.get(sha256(before), 'unknown')
        summary = dict(device=profile.name, mode=mode, before_sha256=sha256(before), baseline=baseline,
                       version_record=u32(metadata, 0x40))
        reporter.log(f'Backup saved ({baseline} firmware, SHA256 {sha256(before)}).')
        if mode == 'backup':
            boot.exchange(7, 0, 0, 0, wait=False)
            reporter.log('Nothing was written; restart sent.')
            return summary
        dat, plain = image
        if baseline == 'unknown':
            raise FlashError('This controller is running firmware the flasher does not recognise, so nothing was '
                             f'erased. Your backup is saved. Install official firmware {profile.official} with '
                             "8BitDo's Firmware Updater, close it, then run the flasher again.")
        if u32(metadata, 0x40) != profile.version:
            raise FlashError('Unexpected firmware version record, so nothing was erased. Your backup is saved. '
                             f'Install official firmware {profile.official} with 8BitDo\'s Firmware Updater first.')
        save(output, 'target.dat', dat)
        save(output, 'expected.bin', plain)
        header = dat[profile.header_offset:profile.header_offset + 28]
        ciphertext = dat[profile.header_offset + 28:profile.header_offset + 28 + profile.size]
        reporter.status('Erasing and writing firmware (keep the cable connected)')
        reporter.log('Backup verified against known firmware. Erasing the application and version record.')
        boot.exchange(4, BASE, profile.size, 0)
        boot.exchange(4, VERSION_ADDRESS, 32, 0)
        written = 0
        while written < profile.size:
            n = min(WRITE_CHUNK, profile.size - written)
            boot.exchange(3, BASE + written, profile.size, n, ciphertext[written:written + n])
            written += n
            reporter.progress(written, profile.size)
        reporter.status('Verifying every byte')
        after = boot.read(BASE, profile.size, reporter.progress)
        save(output, 'after-application.bin', after)
        if after != plain:
            raise FlashError('The firmware did not read back correctly, so the new version was not committed. '
                             'Keep the controller plugged in and reinstall the official firmware with 8BitDo\'s '
                             'Firmware Updater.')
        reporter.log(f'All {profile.size} bytes verified. Committing version {profile.version}.')
        boot.exchange(2, VERSION_ADDRESS, 4, 4, header[:4])
        if u32(boot.read(VERSION_ADDRESS, 4), 0) != profile.version:
            raise FlashError('The firmware version record did not verify.')
        save(output, 'after-metadata.bin', boot.read(META_BASE, META_SIZE))
        boot.exchange(7, 0, 0, 0, wait=False)
        reporter.log('Verified firmware and version; restart sent.')
        summary.update(after_sha256=sha256(after), verified_bytes=profile.size, version_committed=profile.version,
                       reset_sent=True)
        return summary


def run(profile, mode, output, reporter, connect=None):
    if mode not in ('install', 'restore', 'backup'):
        raise FlashError(f'{profile.name} does not support {mode}')
    image = load_image(profile, profile.images[mode]) if mode != 'backup' else None
    if connect is None:
        from . import hidlink
        hidlink.refuse_vendor_updater()
        connect = hidlink.connect
    link = connect(profile, reporter)
    try:
        return session(profile, mode, link, output, reporter, image)
    finally:
        link.close()
