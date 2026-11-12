"""BR23 command allowlist, full reads and the sector writer.

Ported from glassly-ananke-cfw research/backup_controller.py (Transport) and
research/flash_controller.py (read_range, read_full, changed_sectors, Writer) at 0cfd7ee364e8.
The Transport never writes flash; only the Writer arms one exact erase/program command at a time.
"""
import binascii
import os
import struct

from .image import digest, require
from .link import PAGE, ROM_RAM_POLICY, SECTOR, SECTORS, SIZE, WRITE_POLICY, Br23Gates

LOADER_SHA = '7384429e33ee1f340fbffe46aa928a0099c163e1374a40f6f9f27468a6f16ddf'
LOADER_SIZE = 11264
INQUIRY = bytes.fromhex('120000002400')


class Transport:
    """Fail-closed CDB allowlist; identity, pinned RAM loader and flash reads only."""

    def __init__(self, link):
        self.link = link
        self.loader = None
        self.size = None
        self.verified = set()

    def execute(self, cdb, out=None, n=0):
        cdb = bytes(cdb)
        require(0 < len(cdb) <= 16 and n >= 0 and not (out is not None and n), 'Invalid command')
        op = int.from_bytes(cdb[:2], 'big')
        addr = int.from_bytes(cdb[2:6], 'big')
        if cdb == INQUIRY:
            require(out is None and n == 36, 'Invalid inquiry')
        else:
            require(self.loader is not None, 'Identity and loader checks required')
            require(len(cdb) == 16, 'Invalid vendor CDB')
            if op in (0xfb06, 0xfd07):
                length = len(out) if out is not None else n
                off = addr - 0x12000
                require(0 <= off < LOADER_SIZE and off % 512 == 0 and length == 512 and off + length <= LOADER_SIZE, 'Invalid RAM bounds')
                require(int.from_bytes(cdb[6:8], 'big') == length, 'Invalid RAM length')
                if op == 0xfb06:
                    require(out == self.loader[off:off+length] and n == 0, 'Only pinned loader bytes permitted')
                    require(cdb[8:11] == b'\0' + binascii.crc_hqx(out, 0).to_bytes(2, 'little') and cdb[11:] == b'\xff'*5,
                            'Invalid loader CRC/padding')
                    self.verified.discard(off)
                else:
                    require(out is None and cdb[8:] == b'\xff'*8, 'Invalid RAM read')
            elif op == 0xfb08:
                require(cdb[2:] == bytes.fromhex('000120000001') + b'\xff'*8 and out is None and n == 16, 'Invalid RAM jump')
                require(self.verified == set(range(0, LOADER_SIZE, 512)), 'RAM loader not fully verified')
            elif op == 0xfd05:
                require(self.size is not None and out is None and n == 256 and addr % 256 == 0 and addr+n <= self.size,
                        'Invalid flash read bounds')
                require(cdb[6:] == struct.pack('>H', n) + b'\xff'*8, 'Invalid flash read length')
            else:
                require(op in (0xfc0a, 0xfc0b, 0xfc14) and cdb[2:] == b'\xff'*14 and out is None and n == 16,
                        'Forbidden command (no flash/fuse writes)')
        data = self.link.execute(cdb, out=out, n=n)
        require(len(data) == n, 'Short reply')
        if op == 0xfd07:
            require(data == self.loader[addr-0x12000:addr-0x12000+n], 'RAM readback mismatch; stopping')
            self.verified.add(addr-0x12000)
        return data

    def command(self, op, args=b'', out=None, n=16):
        return self.execute((op.to_bytes(2, 'big')+args).ljust(16, b'\xff'), out, 0 if out is not None else n)

    def reply(self, op, args=b''):
        data = self.command(op, args)
        require(data[:2] == op.to_bytes(2, 'big'), 'Response opcode mismatch')
        return data[2:]

    def prepare(self, inquiry, loader, record=None):
        require(inquiry[8:16].strip() == b'BR23' and inquiry[16:32].strip() == b'UBOOT1.00', 'Unsupported bootloader; no RAM code loaded')
        require(len(loader) == LOADER_SIZE and digest(loader) == LOADER_SHA, 'Loader hash mismatch')
        self.loader = loader
        gated = isinstance(self.link, Br23Gates)
        if gated:
            self.link.enable_br23_rom_ram(inquiry, loader)
            if record is not None:
                record(dict(rom_ram_residue_policy=ROM_RAM_POLICY))
        ram_commands = []
        print('BR23 identity and pinned loader verified; uploading 11264 bytes to RAM at 0x12000.', flush=True)
        for off in range(0, len(loader), 512):
            block = loader[off:off+512]
            args = struct.pack('>IH', 0x12000+off, len(block))
            self.command(0xfb06, args+b'\0'+binascii.crc_hqx(block, 0).to_bytes(2, 'little'), out=block)
            if gated:
                ram_commands.append(dict(self.link.last_command))
            self.command(0xfd07, args, n=len(block))
            if gated:
                ram_commands.append(dict(self.link.last_command, data_sha256=digest(block)))
        if record is not None:
            record(dict(loader_sha256=LOADER_SHA, loader_address='0x12000',
                        ram_readback_verified=True, ram_commands=ram_commands))
        print('All 22 RAM blocks match. Starting BR23 loader with SPI NOR argument 1.', flush=True)
        jump_reply = self.reply(0xfb08, bytes.fromhex('000120000001'))
        if record is not None and gated:
            record(dict(loader_jump=dict(self.link.last_command),
                        loader_jump_reply=(b'\xfb\x08'+jump_reply).hex()))
        return self.flash_identity(record)

    def flash_identity(self, record=None):
        responses = []
        commands = []
        gated = isinstance(self.link, Br23Gates)
        for op in (0xfc0a, 0xfc0b, 0xfc14):
            responses.append(self.reply(op))
            if gated:
                commands.append(dict(self.link.last_command, reply=(op.to_bytes(2, 'big')+responses[-1]).hex()))
                if record is not None:
                    record(dict(flash_identity_commands=commands))
        online, fid, buf = responses
        replies = dict(online=online.hex(), flash_id=fid.hex(), buffer_size=buf.hex(), loader_sha256=LOADER_SHA,
                       loader_address='0x12000', ram_readback_verified=True)
        if record is not None:
            record(replies)
        jedec = int.from_bytes(online[2:6], 'little')
        require(online[0] == 3 and 0 < jedec <= 0xffffff, 'Invalid SPI NOR identity')
        require(int.from_bytes(fid[:3], 'big') == jedec >> 8, 'Flash identity replies disagree')
        require(16 <= (jedec & 255) <= 24, 'Unknown flash density')
        require(256 <= int.from_bytes(buf[:4], 'big') <= 65535, 'Invalid loader buffer size')
        self.size = 1 << (jedec & 255)
        if gated:
            self.link.enable_br23_flash_reads(self.size)
        print(f'JEDEC {jedec:06x}; flash size {self.size} bytes.', flush=True)
        return dict(replies, jedec_id=f'{jedec:06x}', size=self.size)


def read_range(transport, address, size):
    return b''.join(transport.command(0xfd05, struct.pack('>IH', a, PAGE), n=PAGE)
                    for a in range(address, address+size, PAGE))


def readonly(path, data):
    with path.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    path.chmod(0o444)


def read_full(transport, path, progress=None):
    with path.open('xb') as f:
        try:
            for a in range(0, SIZE, PAGE):
                f.write(read_range(transport, a, PAGE))
                if a % 0x20000 == 0:
                    print(f'{path.name}: {a:#x}/{SIZE:#x}', flush=True)
                if progress and (a + PAGE) % 0x10000 == 0:
                    progress(a + PAGE, SIZE)
            f.flush()
            os.fsync(f.fileno())
        finally:
            path.chmod(0o444)
    return path.read_bytes()


def changed_sectors(before, desired):
    require(len(before) == len(desired) == SIZE, 'Invalid plan image size')
    changed = {a for a in range(0, SIZE, SECTOR) if before[a:a+SECTOR] != desired[a:a+SECTOR]}
    require(changed <= set(SECTORS), 'Change outside allowed application sectors')
    return tuple(a for a in SECTORS if a in changed)


class Writer:
    def __init__(self, link, reader, before, desired, journal):
        self.link, self.reader = link, reader
        require(len(before) == len(desired) == SIZE, 'Invalid writer image size')
        self.before, self.desired, self.journal = bytes(before), bytes(desired), journal
        self.sectors = changed_sectors(self.before, self.desired)
        self.attempted = set()

    def command(self, cdb, out=None, n=0):
        require(self.link.pending_write is None, 'Nested write')
        self.journal(dict(event='command-start', cdb=cdb.hex(),
                          data_sha256=digest(out) if out is not None else None))
        self.link.pending_write = (cdb, out, n)
        try:
            data = self.link.execute(cdb, out=out, n=n)
            require(len(data) == n, 'Short write acknowledgement')
            if n:
                require(data[:2] == cdb[:2], 'Write acknowledgement opcode mismatch')
            self.link.last_command['residue_policy'] = WRITE_POLICY
            self.journal(dict(event='command-complete', command=self.link.last_command))
            return data
        finally:
            self.link.pending_write = None

    def sector(self, address):
        require(address in self.sectors and address not in self.attempted, 'Invalid/repeated sector')
        require(read_range(self.reader, address, SECTOR) == self.before[address:address+SECTOR],
                'Sector changed since preflight')
        self.attempted.add(address)
        self.command(b'\xfb\x01'+struct.pack('>I', address)+b'\xff'*10, n=16)
        require(read_range(self.reader, address, SECTOR) == b'\xff'*SECTOR,
                'Sector did not erase completely; stopping')
        for a in range(address, address+SECTOR, PAGE):
            block = self.desired[a:a+PAGE]
            cdb = b'\xfb\x04'+struct.pack('>IH', a, PAGE)+b'\0'+binascii.crc_hqx(block, 0).to_bytes(2, 'little')+b'\xff'*5
            self.command(cdb, out=block)
            require(read_range(self.reader, a, PAGE) == block, 'Program readback mismatch; stopping')
        require(read_range(self.reader, address, SECTOR) == self.desired[address:address+SECTOR],
                'Final sector mismatch')
        self.journal(dict(event='sector-verified', address=hex(address),
                          sha256=digest(self.desired[address:address+SECTOR])))
