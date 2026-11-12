"""Partition layout of a Gamebrick flash image, read from the image's own tables.

Two factory layouts have been seen: the 2025 production units (two application slots, VM at
0x7a000) and IINE's current update layout (one slot, VM at 0x34000, an EXIF partition). The
application area, chip key and the storage to preserve all come from the tables, so a unit is
refused only when its tables, bootloader, resources or application are not the known ones.
Ported from glassly-gamebrick-mini-cfw research/flash_layout.py.
"""
import hashlib
import struct

from ..jieli.vendored import chipkeybin_decode, jl_crc16, jl_enc_cipher, jl_sfc_cipher

SIZE = 0x80000
HEADER_KEY = 0xffff
STORAGE_FLAGS = (0x12, 0x92)  # VM, BTIF, EXIF, USERIF: partitions the application owns at runtime
PRODUCTION_2025 = '384c62f51d2c68e1d3199bdbc6599ff139cd4c66defeafa298411eb47de96b7a'
IINE_UPDATE = '44db9021b1dbf99f282741d18d66744c09b1909adb96c38a4a62d1b051d908fd'
# Combinations of flash layout and flash part on which the Glassly and official applications have been
# flashed and seen booting. Any other kind may only be backed up and have its own backup restored: the
# first flash of a new kind happens on a developer-owned unit with recovery proven, never on a user's.
# A unit on the IINE update layout with flash part cd7013 showed no lights after a verified flash on
# 2026-11-13 and could not be recovered.
HARDWARE_VERIFIED = {(PRODUCTION_2025, 0xcd7113)}
LAYOUTS = {PRODUCTION_2025}
LAYOUTS = {
    '384c62f51d2c68e1d3199bdbc6599ff139cd4c66defeafa298411eb47de96b7a':
        'two application slots, VM at 0x7a000 (2025 production units)',
    '44db9021b1dbf99f282741d18d66744c09b1909adb96c38a4a62d1b051d908fd':
        'one application slot, VM at 0x34000, EXIF (IINE update layout)',
}
APPLICATIONS = {
    '902aedc30a84c9ae777e3cbdd12ff5737ac17b7ee3f5288f76c7aa86fe267c97': 'factory',          # May 26 2026 build
    'e858401635ddbc953ef5f6bc5a6d17dff9635a7536bb0d7a91c8d7824ffb21af': 'factory-2025-09',  # Sep 4 2025 build
    '2fb44f4235bd4ed7183439f31ee2a31fb0052d19dd736e20d1d251fd5e1ff259': 'stock',            # IINE update, Oct 29 2025
    '495ce9131d2cadebd558ec6d5ebfd51b6afa1ba957cba5c7f5dd331fcba9f432': 'custom',           # Glassly
    '8b3cf5bff313e27c4da630bf8759df7217f8a8ed88bcbf83d18c280c09b43221': 'withdrawn',        # 2026-10-25; reset loop
}
CFG_SHA = '0ccc2fd579597ca9d91e485457673d7f2525e00d5bfe8d081920e95e4062a0f1'
P11_SHA = 'd37e34cc8200cae2a372c1a83f46c1f65db756984e2470b7437734e7b5c5fc4c'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def entry(data, off):
    require(len(data) >= off + 32, 'Truncated entry')
    e = struct.unpack_from('<HHIIBBH16s', data, off)
    require(jl_crc16(data[off+2:off+32]) == e[0], 'Entry CRC mismatch')
    return e


def name(e):
    return e[-1].split(b'\0')[0].decode('ascii', 'replace')


class Layout:
    """Tables, chip key, application area and storage of one flash image. Parsing never writes."""

    def __init__(self, image):
        require(len(image) == SIZE, 'Incorrect image size')
        self.image = image
        self.top = []
        off = 0x20
        while len(self.top) < 8:
            raw = bytearray(image[off:off+32])
            jl_enc_cipher(raw, 0, 32, HEADER_KEY)
            self.top.append(entry(bytes(raw), 0))
            off += 32
            if off >= self.top[0][2]:  # the table ends where its first file begins
                break
        top = {name(e): e for e in self.top}
        require(name(self.top[0]) == 'uboot.boot' and {'isd_config.ini', 'app_dir_head', 'key_mac'} <= set(top),
                'Unexpected top-level partition table')
        cfg = top['isd_config.ini']
        record = image[cfg[2]:cfg[2]+34]
        require(jl_crc16(record[:32]) == int.from_bytes(record[32:34], 'little'), 'Invalid chip key record')
        self.key = chipkeybin_decode(record[:32])
        self.base = top['app_dir_head'][2]
        head = bytearray(image[self.base:self.base+0x200])
        jl_sfc_cipher(head, 0, 0x200, 0, self.key)
        self.area, self.app, self.cfg = entry(head, 0), entry(head, 32), entry(head, 64)
        require(self.area[2] == 0x1e00120 and self.app[2] == 0x120 and name(self.app) == 'app.bin'
                and name(self.cfg) == 'cfg_tool.bin', 'Unexpected application file table')
        self.partitions = []
        off = 96
        while off + 32 <= self.app[2]:
            try:
                self.partitions.append(entry(head, off))
            except RuntimeError:
                break
            off += 32
        storage = [(e[2], e[2]+e[3]) for e in self.partitions if e[4] in STORAGE_FLAGS and name(e) != 'PRCT']
        storage.append((top['key_mac'][2], top['key_mac'][2]+top['key_mac'][3]))
        self.storage = sorted(storage)
        self.limit = min([e[2] for e in self.top if e[2] > self.base and name(e) != 'app_dir_head']
                         + [s for s, _ in self.storage if s > self.base])
        require(self.base < self.limit <= SIZE and all(s >= self.limit for s, _ in self.storage),
                'Storage overlaps the application area')
        self.plain = bytearray(image)
        jl_sfc_cipher(self.plain, self.base, self.limit-self.base, self.base, self.key)
        self.p11 = entry(self.plain, self.base+self.area[3])
        require(name(self.p11) == 'p11_code.bin', 'Unexpected resource')
        self.used = self.area[3]+self.p11[3]
        masked = bytearray(image)
        masked[self.base:self.limit] = bytes(self.limit-self.base)
        for s, e in self.storage:
            masked[s:e] = bytes(e-s)
        self.fingerprint = digest(masked)

    def application(self):
        return bytes(self.plain[self.base+self.app[2]:self.base+self.app[2]+self.app[3]])

    def config(self):
        return bytes(self.plain[self.base+self.cfg[2]:self.base+self.cfg[2]+self.cfg[3]])

    def resource(self):
        return bytes(self.plain[self.base+self.area[3]+32:self.base+self.used])

    def describe(self):
        return LAYOUTS.get(self.fingerprint, 'unknown layout')


def classify(image):
    """Which known application an image carries, or 'unknown' if anything else is unfamiliar."""
    try:
        layout = Layout(image)
        app = layout.application()
        if (layout.fingerprint in LAYOUTS and digest(layout.config()) == CFG_SHA
                and digest(layout.resource()) == P11_SHA and jl_crc16(app) == layout.app[1]):
            return APPLICATIONS.get(digest(app), 'unknown')
    except (RuntimeError, ValueError, struct.error):
        pass
    return 'unknown'


# sizeof(m_storage) of the application that wrote the USERIF settings record, by application build. The
# record is followed by 0xc8-byte LiteEMF list maps; an application reading a record of the other size
# loads a map header byte as a divisor and resets in a loop (glassly-gamebrick-mini-cfw
# docs/no-boot-investigation.md).
APPLICATION_RECORD_SIZE = {'factory': 0x40, 'stock': 0x40, 'custom': 0x40, 'withdrawn': 0x40, 'factory-2025-09': 0x38}
MAP_MAX = 0xc8


def crc16_modbus(data, crc=0xffff):
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
    return crc


MAPS = 6
SECTOR = 4096
RECORD_INSERT = 0x20
RECORD_GROWTH = 0x40 - 0x38


def valid_map(region):
    # Both builds' check_map: max 0xc8, 8 <= len <= 0xc0, a multiple of 4, CRC-16/MODBUS.
    crc, _, mx, ml = struct.unpack_from('<HHHH', region, 0)
    return mx == MAP_MAX and 8 <= ml <= MAP_MAX - 8 and ml % 4 == 0 and crc16_modbus(region[2:8+ml]) == crc


def userif_partition(layout):
    userif = [e for e in layout.partitions if name(e) == 'USERIF']
    require(len(userif) == 1, 'No USERIF partition')
    return userif[0][2], userif[0][3]


def record_size_of(u):
    """0x38 or 0x40 from where CRC-valid list maps start; 'blank' if erased; 'unknown' otherwise."""
    if all(b == 0xff for b in u):
        return 'blank'
    found = [s for s in (0x38, 0x40)
             if any(valid_map(u[s+i*MAP_MAX:s+(i+1)*MAP_MAX]) for i in range(MAPS) if s+(i+1)*MAP_MAX <= len(u))]
    return found[0] if len(found) == 1 else 'unknown'


def userif_record_size(layout):
    start, size = userif_partition(layout)
    return record_size_of(layout.image[start:start+size])


def migrate_userif(layout):
    """The USERIF partition rewritten from the 0x38 record format to 0x40: eight erased bytes inserted at
    0x20 inside the record's erased span 0x0e..0x20, everything after moved up, so the list maps keep
    their CRCs. Only the first sector changes. Refuses anything it does not fully recognise."""
    start, size = userif_partition(layout)
    u = layout.image[start:start+size]
    require(record_size_of(u) == 0x38, 'USERIF does not hold a 0x38-byte settings record')
    require(all(b == 0xff for b in u[0x0e:0x20]), 'Unexpected data in the reserved span of the settings record')
    require(all(b == 0xff for b in u[0x38+MAPS*MAP_MAX:SECTOR]), 'Unexpected data after the list maps')
    require(u[0x33] != 0, 'The settings byte that becomes the divisor is zero')
    for i in range(MAPS):
        region = u[0x38+i*MAP_MAX:0x38+(i+1)*MAP_MAX]
        require(valid_map(region) or all(b == 0xff for b in region), f'List map {i} is neither valid nor erased')
    migrated = u[:RECORD_INSERT] + b'\xff'*RECORD_GROWTH + u[RECORD_INSERT:SECTOR-RECORD_GROWTH] + u[SECTOR:]
    require(len(migrated) == size and record_size_of(migrated) == 0x40, 'Migrated record did not validate')
    return migrated
