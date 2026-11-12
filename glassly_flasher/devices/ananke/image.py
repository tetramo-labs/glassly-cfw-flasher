"""Application-only plan built from the connected Ananke's own flash.

Ported from glassly-ananke-cfw research/ananke_flasher.py and research/inspect_installed.py at
0cfd7ee364e8. Only the measured BR23 / 1 MiB / 0x0601 layout and the pinned V0900 applications
are supported. Unit keys and all non-application bytes stay on the controller.
"""
import binascii
import hashlib
import json
import struct

from ... import paths
from ..jieli.vendored import chipkeybin_decode, jl_enc_cipher, jl_sfc_cipher

OEM_SHA = '3f4a6bea1e7901188e7c8a202cbe4916d141ec624db66218139d9f98c01762eb'
OEM_BYTES = 338712
CUSTOM_SHA = '833e76739433639e8ad81b3d7d187ac2fe6cd7729088184d8e07ca25f7e0f350'
FACTORY_APP_SHA = '3c52fc8cef33ee1d0d402f68dc9a92ed819d406b5e99a9033f8304de69cfdc6f'
# The first hardware-verified CFW, which lacked the Bluetooth task wake.
FIRST_CFW_SHA = '3f234e58d28903fda3b175297e3d44913c1e7a64919282237343377e4e06f62e'
BOOT_SHA = 'b319dbb4e8ad85c39a68e4aea6f078bcdc25bfde21af6fdc3a49e47073fb6d30'
CFG_SHA = '21334ccee5588a3e72bd871ce0cc3eb2ad3c333f79927842279855fed87f1ec8'
BASE = 0x3020
APP_OFFSET = 0x120
APP_BYTES = 343208
AREA_BYTES = 0x5415c


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def crc(data):
    return binascii.crc_hqx(data, 0)


def entry(data, offset):
    raw = data[offset:offset+32]
    if len(raw) != 32:
        raise ValueError('Truncated entry')
    hc, dc, address, size, flags, reserved, index, name = struct.unpack('<HHIIBBH16s', raw)
    if binascii.crc_hqx(raw[2:], 0) != hc:
        raise ValueError('Entry header checksum mismatch')
    return dict(header_offset=offset, name=name.split(b'\0')[0].decode('ascii'),
                encoded_offset=address, size=size, flags=flags, index=index,
                data_crc16=dc, header_crc_valid=True)


def applications():
    """The official V0900 application (padded as installed) and the patched Glassly one."""
    stock = paths.firmware('ananke/iine-v0900-app.bin')
    require(len(stock) == OEM_BYTES and digest(stock) == OEM_SHA, 'Bundled official application hash mismatch')
    custom = paths.firmware('ananke/glassly-v0900-app.bin')
    require(len(custom) == APP_BYTES and digest(custom) == CUSTOM_SHA, 'Bundled Glassly application hash mismatch')
    return stock.ljust(APP_BYTES, b'\xff'), custom


def layout():
    return json.loads(paths.firmware('ananke/layout-0601.json'))


def decode(image, allowed_apps):
    require(len(image) == 0x100000, 'Expected 1 MiB flash')
    data = bytearray(image)
    header = bytearray(data[:32])
    jl_enc_cipher(header, 0, 32, 0xffff)
    require(crc(header[2:]) == int.from_bytes(header[:2], 'little'), 'Invalid flash header')
    expected = layout()
    key = None
    # All top-level offsets, sizes, flags and indices must match the measured map.
    for ref in expected['top_entries']:
        off = ref['header_offset']
        jl_enc_cipher(data, off, 32, 0xffff)
        got = entry(data, off)
        require(all(got[k] == ref[k] for k in ('name', 'encoded_offset', 'size', 'flags', 'index')),
                'Unsupported top-level partition map')
        if got['name'] == 'isd_config.ini':
            config = data[got['encoded_offset']:got['encoded_offset'] + got['size']]
            require(crc(config) == got['data_crc16'] and crc(config[:32]) == int.from_bytes(config[32:34], 'little'),
                    'Invalid local chip-key record')
            key = chipkeybin_decode(config[:32])
    require(key is not None, 'Missing chip-key record')
    # Decode the pinned bootloader's bank container; never execute unknown code.
    boot = bytearray(image[0xc0:0xc0 + 8168])
    count, index = 1, 0
    while index < count:
        off = index * 16
        require(off + 16 <= len(boot), 'Invalid boot bank table')
        jl_enc_cipher(boot, off, 16, 0xffff)
        hdr = boot[off:off+14]
        require(crc(hdr) == int.from_bytes(boot[off+14:off+16], 'little'), 'Invalid boot bank header')
        bankid, size, address, start, checksum = struct.unpack('<HHIIH', hdr)
        if index == 0:
            count = bankid
            require(1 <= count <= 32, 'Invalid boot bank count')
        require(start >= count * 16 and start + size <= len(boot), 'Invalid boot bank bounds')
        jl_enc_cipher(boot, start, size, 0xffff)
        require(crc(boot[start:start+size]) == checksum, 'Boot bank checksum mismatch')
        index += 1
    require(digest(boot) == BOOT_SHA, 'Unknown bootloader')
    jl_sfc_cipher(data, BASE, AREA_BYTES, BASE, key)
    for ref in [expected['application_area'], *expected['application_entries']]:
        got = entry(data, ref['header_offset'])
        require(all(got[k] == ref[k] for k in ('name', 'encoded_offset', 'size', 'flags', 'index')),
                'Unsupported application partition map')
    area, app, cfg = (entry(data, BASE + off) for off in (0, 32, 64))
    require(crc(data[BASE+32:BASE+AREA_BYTES]) == area['data_crc16'], 'Application area CRC mismatch')
    installed = data[BASE+APP_OFFSET:BASE+APP_OFFSET+APP_BYTES]
    config = data[BASE+cfg['encoded_offset']:BASE+AREA_BYTES]
    require(crc(installed) == app['data_crc16'] and digest(installed) in allowed_apps, 'Unknown installed application')
    require(crc(config) == cfg['data_crc16'] and digest(config) == CFG_SHA, 'Unknown controller configuration')
    return data, key


def plan(image, stock, custom, mode):
    require(mode in ('install', 'restore'), 'Invalid operation')
    require(len(stock) == APP_BYTES and digest(stock[:OEM_BYTES]) == OEM_SHA and stock[OEM_BYTES:] == b'\xff' * (APP_BYTES-OEM_BYTES),
            'Unpinned stock application')
    require(len(custom) == APP_BYTES and digest(custom) == CUSTOM_SHA, 'Unpinned custom application')
    allowed = {FACTORY_APP_SHA, digest(stock), CUSTOM_SHA, FIRST_CFW_SHA}
    data, key = decode(image, allowed)
    replacement = custom if mode == 'install' else stock
    data[BASE+APP_OFFSET:BASE+APP_OFFSET+APP_BYTES] = replacement
    for off, checksum in ((BASE+32, crc(replacement)), (BASE, None)):
        if checksum is None:
            checksum = crc(data[BASE+32:BASE+AREA_BYTES])
        struct.pack_into('<H', data, off+2, checksum)
        struct.pack_into('<H', data, off, crc(data[off+2:off+32]))
    jl_sfc_cipher(data, BASE, AREA_BYTES, BASE, key)
    # Only insert the used active area; top-level decoding must never reach flash.
    desired = image[:BASE] + bytes(data[BASE:BASE+AREA_BYTES]) + image[BASE+AREA_BYTES:]
    check, _ = decode(desired, {digest(replacement)})
    require(check[BASE+APP_OFFSET:BASE+APP_OFFSET+APP_BYTES] == replacement, 'Application roundtrip mismatch')
    original_decoded, _ = decode(image, allowed)
    require(check[BASE+64:BASE+APP_OFFSET] == original_decoded[BASE+64:BASE+APP_OFFSET] and
            check[BASE+APP_OFFSET+APP_BYTES:BASE+AREA_BYTES] == original_decoded[BASE+APP_OFFSET+APP_BYTES:BASE+AREA_BYTES],
            'Plan changed configuration or reserved records')
    return desired
