"""Gamebrick Mini images, derived from the connected unit's own flash.

Ported from glassly-gamebrick-mini-cfw research/stock_update.py (build) and
research/gamebrick_flasher.py (fingerprints and plan) at 9d51cdf93e5b. The Glassly and official
applications ship hash-pinned in firmware/gamebrick; nothing is downloaded or rebuilt here.
VM/BTIF/USERIF and the key/MAC sector are never written.
"""
import hashlib
import struct

from ... import paths
from ..jieli.vendored import jl_crc16, jl_sfc_cipher

OEM_SHA = '2fb44f4235bd4ed7183439f31ee2a31fb0052d19dd736e20d1d251fd5e1ff259'
CUSTOM_APP_SHA = '495ce9131d2cadebd558ec6d5ebfd51b6afa1ba957cba5c7f5dd331fcba9f432'
BASE = 0x2020
LIMIT = 0x3e020
KNOWN_APP_SIZES = (196224, 198824, 199232)
STORAGE_START, SIZE = 0x7a000, 0x80000  # VM, BTIF, USERIF, then the (blank) key/MAC sector

# SHA256 of a flash image with its runtime storage and key/MAC sector zeroed: the same for every
# unit seen so far, because only those regions differ between controllers.
FINGERPRINTS = {
    '62153ccb490b114cc604501526a72c505c27796b1778b07761fab156985a45d9': 'factory',
    'dc16ee8a3dba5468cc7ad3b0af4e7a714d018a4b9eda929d877d233ec9b4958c': 'stock',
    'c482ad55d3fab1086d889673fd8dd9a832ba1ccc6188bad71bf4f90597a0c5bc': 'custom',
    'acc50d0b7b83e3e02480ff3ec87c8c0ee474489f59f4eb33bd22eeb6f5cd2099': 'withdrawn',  # 2026-10-25 image; reset loop
}


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


def header_crc(data, off):
    struct.pack_into('<H', data, off, jl_crc16(data[off+2:off+32]))


def build(original, replacement, original_sha):
    require(len(original) == SIZE and digest(original) == original_sha, 'Wrong original backup')
    plain = bytearray(original)
    jl_sfc_cipher(plain, BASE, LIMIT-BASE, BASE, 0x190d)
    area = entry(plain, BASE)
    app = entry(plain, BASE+32)
    cfg = entry(plain, BASE+64)
    # Factory (196224), official update (198824) or Glassly custom (199232) application.
    require(area[2] == 0x1e00120 and app[2] == 0x120 and app[3] in KNOWN_APP_SIZES, 'Unexpected application layout')
    require(app[-1].startswith(b'app.bin\0') and cfg[-1].startswith(b'cfg_tool.bin\0'), 'Unexpected file table')
    p11 = entry(plain, BASE+area[3])
    used = area[3]+p11[3]
    require(p11[-1].startswith(b'p11_code.bin\0'), 'Unexpected resource')
    region = plain[BASE:BASE+used]
    require(jl_crc16(region[32:area[3]]) == area[1], 'Application area CRC mismatch')
    require(jl_crc16(region[app[2]:app[2]+app[3]]) == app[1], 'Application CRC mismatch')
    require(cfg[2] == app[2]+app[3] and cfg[2]+cfg[3] == area[3], 'Unexpected file gaps')
    require(jl_crc16(region[cfg[2]:cfg[2]+cfg[3]]) == cfg[1], 'Configuration CRC mismatch')
    require(jl_crc16(region[area[3]+32:used]) == p11[1], 'P11 CRC mismatch')
    growth = len(replacement)-app[3]
    require(BASE+used+growth < LIMIT, 'Update exceeds active area')
    updated = region[:app[2]]+replacement+region[app[2]+app[3]:]
    struct.pack_into('<H', updated, 34, jl_crc16(replacement))
    struct.pack_into('<I', updated, 40, len(replacement))
    header_crc(updated, 32)
    struct.pack_into('<I', updated, 68, cfg[2]+growth)
    header_crc(updated, 64)
    struct.pack_into('<I', updated, 8, area[3]+growth)
    struct.pack_into('<H', updated, 2, jl_crc16(updated[32:area[3]+growth]))
    header_crc(updated, 0)
    require(updated[96:app[2]] == region[96:app[2]], 'Partition entries changed')
    require(updated[cfg[2]+growth:] == region[cfg[2]:], 'Config or P11 changed')
    output = bytearray(original)
    output[BASE:BASE+len(updated)] = updated
    jl_sfc_cipher(output, BASE, len(updated), BASE, 0x190d)
    # A smaller application frees the tail of the active area; leave it erased, as the factory image does.
    if growth < 0:
        output[BASE+len(updated):BASE+used] = b'\xff'*-growth
    end = BASE+max(len(updated), used)
    require(output[:BASE] == original[:BASE] and output[end:] == original[end:], 'Unrelated bytes changed')
    return bytes(output)


def fingerprint(image):
    require(len(image) == SIZE, 'Incorrect image size')
    masked = bytearray(image)
    masked[STORAGE_START:SIZE] = bytes(SIZE - STORAGE_START)
    return hashlib.sha256(masked).hexdigest()


def classify(image):
    return FINGERPRINTS.get(fingerprint(image), 'unknown')


def official_application():
    app = paths.firmware('gamebrick/iine-official-app.bin')
    require(digest(app) == OEM_SHA, 'Bundled official application hash mismatch')
    return app


def glassly_application():
    app = paths.firmware('gamebrick/glassly-gamebrick-app.bin')
    require(digest(app) == CUSTOM_APP_SHA, 'Bundled Glassly application hash mismatch')
    return app


def plan(current, oem, mode, custom=None):
    """Source and target images for `mode` from the live flash, both carrying its storage."""
    state = classify(current)
    require(state in ('factory', 'stock', 'custom'),
            f'Unrecognised flash contents ({state}); refusing to write. Keep the saved backup and ask for help.')
    sha = digest(current)
    stock = build(current, oem, sha)
    if mode == 'install':
        target = build(current, custom or glassly_application(), sha)
    elif mode == 'restore':
        target = stock
    else:
        raise RuntimeError('Invalid operation')
    require(FINGERPRINTS.get(fingerprint(target)) == ('custom' if mode == 'install' else 'stock'),
            'Derived image does not match the reviewed fingerprint')
    require(target[STORAGE_START:] == current[STORAGE_START:], 'Plan would alter runtime storage or key/MAC')
    sectors = [a for a in range(0, SIZE, 4096) if current[a:a+4096] != target[a:a+4096]]
    return {'state': state, 'target': target, 'stock': stock, 'changed_sectors': sectors,
            'current_sha256': sha, 'target_sha256': digest(target), 'stock_sha256': digest(stock)}
