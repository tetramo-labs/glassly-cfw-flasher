"""Gamebrick Mini images, derived from the connected unit's own flash.

Ported from glassly-gamebrick-mini-cfw research/stock_update.py (build) and
research/gamebrick_flasher.py (plan). The unit's partition tables (layout.py) give the
application area, chip key and storage partitions; the Glassly and official applications ship
hash-pinned in firmware/gamebrick. Only the application area is ever written.
"""
import hashlib
import struct

from ... import paths
from ..jieli.vendored import jl_crc16, jl_sfc_cipher
from .layout import BOOT_VERIFIED_LAYOUTS, LAYOUTS, Layout, classify

OEM_SHA = '2fb44f4235bd4ed7183439f31ee2a31fb0052d19dd736e20d1d251fd5e1ff259'
CUSTOM_APP_SHA = '495ce9131d2cadebd558ec6d5ebfd51b6afa1ba957cba5c7f5dd331fcba9f432'
KNOWN_APP_SIZES = (196224, 198760, 198824, 199232)
SIZE = 0x80000


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
    layout = Layout(original)
    require(layout.fingerprint in LAYOUTS, 'Unknown flash layout')
    base, limit, key = layout.base, layout.limit, layout.key
    plain = bytearray(original)
    jl_sfc_cipher(plain, base, limit-base, base, key)
    area = entry(plain, base)
    app = entry(plain, base+32)
    cfg = entry(plain, base+64)
    # Factory (196224 or 198760), official update (198824) or Glassly custom (199232) application.
    require(area[2] == 0x1e00120 and app[2] == 0x120 and app[3] in KNOWN_APP_SIZES, 'Unexpected application layout')
    require(app[-1].startswith(b'app.bin\0') and cfg[-1].startswith(b'cfg_tool.bin\0'), 'Unexpected file table')
    p11 = entry(plain, base+area[3])
    used = area[3]+p11[3]
    require(p11[-1].startswith(b'p11_code.bin\0'), 'Unexpected resource')
    region = plain[base:base+used]
    require(jl_crc16(region[32:area[3]]) == area[1], 'Application area CRC mismatch')
    require(jl_crc16(region[app[2]:app[2]+app[3]]) == app[1], 'Application CRC mismatch')
    require(cfg[2] == app[2]+app[3] and cfg[2]+cfg[3] == area[3], 'Unexpected file gaps')
    require(jl_crc16(region[cfg[2]:cfg[2]+cfg[3]]) == cfg[1], 'Configuration CRC mismatch')
    require(jl_crc16(region[area[3]+32:used]) == p11[1], 'P11 CRC mismatch')
    growth = len(replacement)-app[3]
    require(base+used+growth < limit, 'Update exceeds active area')
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
    output[base:base+len(updated)] = updated
    jl_sfc_cipher(output, base, len(updated), base, key)
    # A smaller application frees the tail of the active area; leave it erased, as the factory image does.
    if growth < 0:
        output[base+len(updated):base+used] = b'\xff'*-growth
    end = base+max(len(updated), used)
    require(output[:base] == original[:base] and output[end:] == original[end:], 'Unrelated bytes changed')
    return bytes(output)


def official_application():
    app = paths.firmware('gamebrick/iine-official-app.bin')
    require(digest(app) == OEM_SHA, 'Bundled official application hash mismatch')
    return app


def glassly_application():
    app = paths.firmware('gamebrick/glassly-gamebrick-app.bin')
    require(digest(app) == CUSTOM_APP_SHA, 'Bundled Glassly application hash mismatch')
    return app


KNOWN_STATES = ('factory', 'factory-2025-09', 'stock', 'custom')


def plan(current, oem, mode, custom=None, backup=None, allow_unverified=False):
    """Source and target images for `mode` from the live flash, both carrying its storage.
    `restore-backup` puts back the application found in `backup`, an earlier full read of this
    same kind of controller."""
    state = classify(current)
    require(state in KNOWN_STATES,
            f'Unrecognised flash contents ({state}); refusing to write. Keep the saved backup and ask for help.')
    layout = Layout(current)
    if mode in ('install', 'restore') and not allow_unverified:
        require(layout.fingerprint in BOOT_VERIFIED_LAYOUTS,
                'The Glassly and official applications have not been shown to boot on this flash layout: a '
                'controller of this kind did not start after a verified flash on 2026-11-13. Only "restore the '
                'controller\'s own backup" is allowed on it until that is understood. Nothing was written.')
    sha = digest(current)
    stock = build(current, oem, sha)
    if mode == 'install':
        target, expected = build(current, custom or glassly_application(), sha), 'custom'
    elif mode == 'restore':
        target, expected = stock, 'stock'
    elif mode == 'restore-backup':
        require(backup is not None and len(backup) == SIZE, 'A full 512 KiB backup read is required')
        expected = classify(backup)
        require(expected in KNOWN_STATES and Layout(backup).fingerprint == layout.fingerprint,
                f'The backup is not a known image from this kind of controller ({expected})')
        target = build(current, Layout(backup).application(), sha)
    else:
        raise RuntimeError('Invalid operation')
    require(classify(target) == expected, 'Derived image does not carry the reviewed application')
    require(target[:layout.base] == current[:layout.base] and target[layout.limit:] == current[layout.limit:],
            'Plan would alter bytes outside the application area')
    sectors = [a for a in range(0, SIZE, 4096) if current[a:a+4096] != target[a:a+4096]]
    return {'state': state, 'layout': layout.describe(), 'chip_key': f'{layout.key:#06x}', 'target': target,
            'stock': stock, 'target_state': expected, 'changed_sectors': sectors, 'current_sha256': sha,
            'target_sha256': digest(target), 'stock_sha256': digest(stock)}
