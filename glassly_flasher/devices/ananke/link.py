"""BR23 status/residue gates, shared by the macOS Bulk-Only link and the Windows/Linux pass-through.

Ported from glassly-ananke-cfw research/usb_transport.py (NativeLink gates) and
research/flash_controller.py (InstallLink write gates) at 0cfd7ee364e8. The BR23 ROM and the
pinned loader report fixed nonzero residues for some commands; each is accepted only for the
exact command shape and session state it was observed in (docs/evidence/br23-residue.json there).
"""
import binascii
import hashlib
import struct

from ..jieli.link import INQUIRY, BulkOnlyLink, LinkError, PassThroughLink

BR23_INQUIRY = bytes.fromhex('0080020220000000425232332020202055424f4f54312e303020202020202020312e3030')
BR23_LOADER_SHA = '7384429e33ee1f340fbffe46aa928a0099c163e1374a40f6f9f27468a6f16ddf'
ROM_RAM_POLICY = 'br23-uboot100-pinned-ram-full-residue-v1'
ROM_JUMP_POLICY = 'br23-uboot100-verified-jump-full-residue-v1'
LOADER_QUERY_POLICY = 'br23-verified-loader-identity-query-full-residue-v1'
FLASH_READ_POLICY = 'br23-856014-bounded-flash-full-residue-v1'
WRITE_POLICY = 'br23-pinned-loader-application-write-full-residue-v1'
VENDOR, PRODUCT = 0x4c4a, 0x2342
SIZE, SECTOR, PAGE = 0x100000, 4096, 256
SECTORS = tuple(range(0x4000, 0x57000, SECTOR)) + (0x3000,)


class Br23Gates:
    """Session state and per-command residue expectations; writes only when armed by the Writer."""

    def reset_gates(self):
        self.rom_loader = None
        self.rom_verified = set()
        self.loader_queries_enabled = False
        self.loader_query_replies = {}
        self.flash_read_size = None
        self.pending_write = None

    def enable_br23_rom_ram(self, inquiry, loader):
        # Exact locally observed identity and pinned blob.
        if (inquiry != BR23_INQUIRY or len(loader) != 11264 or
                hashlib.sha256(loader).hexdigest() != BR23_LOADER_SHA):
            raise LinkError('BR23 ROM residue profile identity/loader mismatch')
        self.rom_loader = bytes(loader)
        self.rom_verified = set()
        self.loader_queries_enabled = False
        self.loader_query_replies = {}
        self.flash_read_size = None

    def enable_br23_flash_reads(self, size):
        self.flash_read_size = None
        replies = self.loader_query_replies
        if not (self.loader_queries_enabled and set(replies) == {0xfc0a, 0xfc0b, 0xfc14}):
            raise LinkError('Verified loader identity queries required before flash reads')
        online, fid, buf = (replies[op][2:] for op in (0xfc0a, 0xfc0b, 0xfc14))
        # Scope the observed full-residue quirk to this measured flash identity.
        if not (online[0] == 3 and int.from_bytes(online[2:6], 'little') == 0x856014 and
                fid[:3] == bytes.fromhex('008560') and
                int.from_bytes(buf[:4], 'big') == 0x8000 and size == 0x100000):
            raise LinkError(f'Unsupported flash: the loader reports device type {online[0]:#04x}, flash ID '
                            f'{int.from_bytes(online[2:6], "little"):#08x}, read-ID {fid[:3].hex()}, buffer '
                            f'{int.from_bytes(buf[:4], "big")} bytes; the supported controllers have SPI NOR flash ID '
                            '0x856014 with a 32768-byte buffer. Nothing was written. Report this message so this '
                            'flash chip can be checked.')
        self.flash_read_size = size

    def expected_residue(self, cdb, out, n):
        op = int.from_bytes(cdb[:2], 'big')
        if op in (0xfb01, 0xfb04):
            if self.flash_read_size != SIZE or self.pending_write != (cdb, out, n):
                raise LinkError('Write not armed for this exact command/data')
            addr = int.from_bytes(cdb[2:6], 'big')
            if op == 0xfb01:
                valid = (addr in SECTORS and out is None and n == 16 and
                         cdb == b'\xfb\x01' + struct.pack('>I', addr) + b'\xff'*10)
            else:
                valid = (out is not None and len(out) == PAGE and n == 0 and addr % PAGE == 0 and
                         addr // SECTOR * SECTOR in SECTORS and
                         cdb == b'\xfb\x04'+struct.pack('>IH', addr, PAGE)+b'\0' +
                         binascii.crc_hqx(out, 0).to_bytes(2, 'little')+b'\xff'*5)
            if not valid:
                raise LinkError('Invalid sector/page write framing or bounds')
            # The pinned loader consumes the data without updating the transfer count, as on its
            # reads. Full residue, status and exact readback are all still required.
            return n if out is None else len(out)
        if cdb != INQUIRY and op not in (0xfb06, 0xfd07, 0xfb08, 0xfc0a, 0xfc0b, 0xfc14, 0xfd05):
            raise LinkError('Command forbidden by the Ananke installer')
        if self.flash_read_size is not None and cdb[:2] == b'\xfd\x05':
            address = int.from_bytes(cdb[2:6], 'big')
            if not (len(cdb) == 16 and out is None and n == 256 and address % 256 == 0 and
                    address + n <= self.flash_read_size and cdb[6:] == b'\x01\x00'+b'\xff'*8):
                raise LinkError('Invalid BR23 flash read bounds/framing')
            return 256
        if self.loader_queries_enabled and cdb[:2] in (b'\xfc\x0a', b'\xfc\x0b', b'\xfc\x14'):
            if not (len(cdb) == 16 and cdb[2:] == b'\xff'*14 and out is None and n == 16):
                raise LinkError('Invalid BR23 loader identity query')
            return 16
        if self.rom_loader is None:
            return 0
        if op == 0xfb08:
            if not (cdb == bytes.fromhex('fb08000120000001') + b'\xff'*8 and
                    out is None and n == 16 and self.rom_verified == set(range(0, 11264, 512))):
                raise LinkError('BR23 jump requires the exact fully verified loader')
            # Retire the ROM policy before any post-jump loader commands.
            self.rom_loader = None
            self.rom_verified = set()
            return 16
        if op not in (0xfb06, 0xfd07):
            return 0
        address = int.from_bytes(cdb[2:6], 'big')
        offset = address - 0x12000
        if not (len(cdb) == 16 and 0 <= offset <= 11264 - 512 and offset % 512 == 0 and
                cdb[6:8] == b'\x02\x00'):
            raise LinkError('Invalid BR23 ROM RAM command')
        if op == 0xfb06:
            block = self.rom_loader[offset:offset + 512]
            if not (out == block and n == 0 and cdb[8:] == b'\0' +
                    binascii.crc_hqx(block, 0).to_bytes(2, 'little') + b'\xff' * 5):
                raise LinkError('Only pinned BR23 loader writes are covered')
            self.rom_verified.discard(offset)
        elif not (out is None and n == 512 and cdb[8:] == b'\xff' * 8):
            raise LinkError('Invalid BR23 ROM RAM read')
        # This ROM leaves the requested count in the status for FB06/FD07. Every RAM block is
        # still compared byte-for-byte before any execution.
        return 512

    def residue_policy(self, cdb, expected):
        return (ROM_RAM_POLICY if expected == 512 else
                FLASH_READ_POLICY if expected == 256 else
                ROM_JUMP_POLICY if expected == 16 and cdb[:2] == b'\xfb\x08' else
                LOADER_QUERY_POLICY if expected == 16 else 'standard-zero')

    def accept(self, cdb, data, expected):
        if expected == 512 and cdb[:2] == b'\xfd\x07':
            offset = int.from_bytes(cdb[2:6], 'big') - 0x12000
            if self.rom_loader is None or data != self.rom_loader[offset:offset+512]:
                raise LinkError('RAM readback mismatch; stopping')
            self.rom_verified.add(offset)
        if expected == 16:
            if data[:2] != cdb[:2]:
                raise LinkError('BR23 acknowledgement mismatch')
            if cdb[:2] == b'\xfb\x08':
                # Enabled only after exact identity, all 22 RAM readbacks and the pinned jump.
                self.loader_queries_enabled = True
            elif self.loader_queries_enabled and int.from_bytes(cdb[:2], 'big') in (0xfc0a, 0xfc0b, 0xfc14):
                self.flash_read_size = None
                self.loader_query_replies[int.from_bytes(cdb[:2], 'big')] = data


class Br23BulkOnlyLink(Br23Gates, BulkOnlyLink):
    def __init__(self, native, vendor=VENDOR, product=PRODUCT):
        BulkOnlyLink.__init__(self, native, vendor, product)
        self.reset_gates()

    @classmethod
    def load(cls, vendor=VENDOR, product=PRODUCT):
        return cls(cls.library(), vendor, product)


class Br23PassThroughLink(Br23Gates, PassThroughLink):
    def __init__(self, dev_class=None, candidates=None, platform=None):
        PassThroughLink.__init__(self, lambda inquiry: inquiry == BR23_INQUIRY, dev_class, candidates, platform)
        self.reset_gates()
