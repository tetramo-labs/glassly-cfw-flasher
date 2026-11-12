"""BD19 command allowlist and the sector programmer.

Ported from glassly-gamebrick-mini-cfw research/flash_stock.py (Transport, program_image) and
research/flash_gamebrick.py (program_with_plan) at 9d51cdf93e5b.
"""
import struct

from ..jieli import loader_blob
from ..jieli.vendored import jl_crc16
from .image import SIZE, digest, require

SECTOR = 4096
PAGE = 256
LOADER_SHA = 'faa1b519a3ff1b082f11ccafac8c73d31150161ac972ef9f1934002bcbb8df4f'


class Transport:
    """Command allowlist and bounds over a platform link. Every CDB is checked here first."""

    def __init__(self, link, sectors):
        self.link = link
        self.sectors = frozenset(sectors)
        self.writes_enabled = False

    def execute(self, cdb, out=None, n=0):
        require(0 < len(cdb) <= 16 and not (out is not None and n), 'Invalid command')
        op = int.from_bytes(cdb[:2], 'big')
        addr = int.from_bytes(cdb[2:6], 'big')
        if cdb == bytes.fromhex('120000002400'):
            require(out is None and n == 36, 'Invalid inquiry')
        elif op in (0xfb01, 0xfb04):
            require(self.writes_enabled, 'Writes have not been authorized by full-image checks')
            require(addr//SECTOR*SECTOR in self.sectors, 'Write outside selected sectors')
            if op == 0xfb01:
                require(addr % SECTOR == 0 and out is None and n == 16, 'Invalid sector erase')
            else:
                require(out is not None and len(out) == PAGE and addr % PAGE == 0 and
                        int.from_bytes(cdb[6:8], 'big') == PAGE, 'Invalid flash page')
        elif op in (0xfb06, 0xfd07):
            length = len(out) if out is not None else n
            require(0x2000 <= addr and addr+length <= 0x2000+23040 and length <= 512, 'Invalid loader RAM access')
            require(int.from_bytes(cdb[6:8], 'big') == length, 'Invalid RAM length')
            require((op == 0xfb06 and out is not None) or (op == 0xfd07 and out is None), 'Wrong RAM direction')
        elif op == 0xfb08:
            require(cdb[2:8] == bytes.fromhex('000020000001') and out is None and n == 16, 'Invalid RAM jump')
        elif op == 0xfd05:
            require(0 <= addr and addr+n <= SIZE and n == PAGE and out is None and
                    int.from_bytes(cdb[6:8], 'big') == n, 'Invalid flash read')
        else:
            require(op in (0xfc0a, 0xfc0b, 0xfc14) and out is None and n == 16, 'Forbidden command')
        data = self.link.execute(cdb, out, n)
        require(len(data) == n, f'Short reply: {len(data)}/{n}')
        return data

    def command(self, op, args=b'', out=None, n=16):
        return self.execute((op.to_bytes(2, 'big')+args).ljust(16, b'\xff'), out, n=0 if out is not None else n)

    def reply(self, op, args=b''):
        data = self.command(op, args)
        require(data[:2] == op.to_bytes(2, 'big'), 'Unexpected command response')
        return data[2:]

    def initialize(self, loader=None):
        inquiry = self.execute(bytes.fromhex('120000002400'), n=36)
        require(inquiry[8:16].decode('ascii', 'replace').strip() == 'BD19' and
                inquiry[16:32].decode('ascii', 'replace').strip() == 'UBOOT1.00', 'Wrong controller bootloader')
        loader = loader_blob('bd19loader.bin') if loader is None else loader
        require(len(loader) == 23040 and digest(loader) == LOADER_SHA, 'RAM loader hash mismatch')
        print('BD19 identity confirmed. Loading and verifying RAM loader.', flush=True)
        for off in range(0, len(loader), 512):
            block = loader[off:off+512]
            addr = 0x2000+off
            self.command(0xfb06, struct.pack('>IH', addr, len(block))+b'\0'+jl_crc16(block).to_bytes(2, 'little'), out=block)
            require(self.command(0xfd07, struct.pack('>IH', addr, len(block)), n=len(block)) == block, 'RAM loader readback mismatch')
        self.reply(0xfb08, bytes.fromhex('000020000001'))
        online = self.reply(0xfc0a)
        fid = self.reply(0xfc0b)
        buf = self.reply(0xfc14)
        require(online[0] == 3 and int.from_bytes(online[2:6], 'little') == 0xcd7113, 'Wrong flash device')
        require(int.from_bytes(fid[:3], 'big') == 0xcd71, 'Flash ID responses disagree')
        require(int.from_bytes(buf[:4], 'big') >= PAGE, 'Insufficient loader buffer')

    def read(self, addr, size):
        require(addr % PAGE == 0 and size % PAGE == 0, 'Read must be page aligned')
        return b''.join(self.command(0xfd05, struct.pack('>IH', a, PAGE), n=PAGE) for a in range(addr, addr+size, PAGE))

    def erase(self, addr):
        self.reply(0xfb01, addr.to_bytes(4, 'big'))

    def program(self, addr, data):
        self.command(0xfb04, struct.pack('>IH', addr, len(data))+b'\0'+jl_crc16(data).to_bytes(2, 'little'), out=data)


def read_flash(device, progress=None, chunk=0x10000):
    parts = []
    for a in range(0, SIZE, chunk):
        parts.append(device.read(a, chunk))
        if progress:
            progress(a+chunk, SIZE)
    return b''.join(parts)


def program_image(device, original, target, mode, before=None, progress=None):
    require(len(original) == len(target) == SIZE, 'Incorrect image size')
    sectors = [a for a in range(0, SIZE, SECTOR) if original[a:a+SECTOR] != target[a:a+SECTOR]]
    require(sectors and min(sectors) >= 0x2000 and max(sectors)+SECTOR <= 0x34000, 'Unexpected update extent')
    print('Reading all flash before allowing writes.', flush=True)
    current = read_flash(device) if before is None else before
    require(len(current) == SIZE, 'Incomplete pre-write read')
    desired = target if mode == 'install' else original
    if current == desired:
        print('Already matches requested image; no writes needed.', flush=True)
        return {'writes': 0, 'sha256': digest(current)}
    if mode == 'install':
        require(current == original, 'Installed flash differs from the verified backup; no writes allowed')
    else:
        for a in range(0, SIZE, SECTOR):
            if a not in sectors:
                require(current[a:a+SECTOR] == original[a:a+SECTOR],
                        f'Protected sector {a:#x} changed; refusing recovery on unidentified state')
    device.writes_enabled = True
    # Commit the active application header last. This is not atomic across power loss.
    ordered = sectors[1:]+sectors[:1]
    count = 0
    for a in ordered:
        block = desired[a:a+SECTOR]
        if current[a:a+SECTOR] == block:
            continue
        require(device.read(a, SECTOR) == current[a:a+SECTOR], f'Sector {a:#x} changed since preflight')
        device.erase(a)
        require(device.read(a, SECTOR) == b'\xff'*SECTOR, f'Sector erase failed at {a:#x}')
        for off in range(0, SECTOR, PAGE):
            page = block[off:off+PAGE]
            if page != b'\xff'*PAGE:
                device.program(a+off, page)
        require(device.read(a, SECTOR) == block, f'Sector verification failed at {a:#x}')
        count += 1
        print(f'Verified sector {a:#07x} ({count}/{len(sectors)})', flush=True)
        if progress:
            progress(count, len(sectors))
    print('Reading all flash to verify.', flush=True)
    final = read_flash(device)
    require(final == desired, 'Full flash readback differs from intended image')
    return {'writes': count, 'sha256': digest(final), 'full_readback_verified': True}


def program_with_plan(device, source_and_target, save, progress=None):
    """Read twice, plan from the first read, then program images that already carry the live storage."""
    require(not device.writes_enabled, 'Unexpected enabled writer')
    print('Reading all flash twice before allowing writes.', flush=True)
    first = read_flash(device, progress)
    require(len(first) == SIZE, 'Incomplete first flash read')
    save('flash-pass1.bin', first)
    second = read_flash(device, progress)
    require(second == first, 'Flash changed between preflight reads; no writes allowed')
    save('flash-pass2.bin', second)
    source, target = source_and_target(first)
    if first == target:
        print('Already running the requested firmware; nothing to write.', flush=True)
        return {'writes': 0, 'sha256': digest(first), 'full_readback_verified': True}
    device.sectors = frozenset(a for a in range(0, SIZE, SECTOR) if source[a:a+SECTOR] != target[a:a+SECTOR])
    try:
        return dict(program_image(device, source, target, 'install', before=first, progress=progress), matching_full_reads=2)
    finally:
        device.writes_enabled = False
