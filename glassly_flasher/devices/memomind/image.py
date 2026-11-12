"""The bundled MemoMind One image and the ME/WuQi container checks from the research repo's
scripts/memomind_ota.py (unpack_ota at e56172a3). These prove the container is intact, not that it boots;
only the SHA256 pin admits an image."""
import hashlib
import lzma
import struct
import zlib

from ... import paths
from ..base import FlashError

CFW = 'memomind/glassly-memomind-one-cfw-25.bin'
CFW_SHA256 = '9cdd04786fceee283c30dafc20a01d680894d60026fdc78dbb7a8195fe54fae1'
CFW_SIZE = 2243544
CFW_REVISION = 25

OUTER_SIZE = 18
STREAM_START = 0x96
TABLE_START = 0x32
HEADER_CRC = 0x92
COMPONENTS = 6


def crc(data):
    return zlib.crc32(data) & 0xffffffff


def require(condition, message):
    if not condition:
        raise FlashError(f'Bundled MemoMind firmware failed its container check: {message}')


def check_container(data):
    require(len(data) >= STREAM_START + 4, 'container too short')
    require(data[:6] == b'ME\x02\x01\x10\x01', 'unsupported ME envelope profile')
    require(struct.unpack_from('>I', data, 6)[0] == len(data) - OUTER_SIZE, 'ME payload length mismatch')
    require(struct.unpack_from('>I', data, 14)[0] == crc(data[OUTER_SIZE:]), 'ME payload CRC32 mismatch')
    require(struct.unpack_from('<I', data, 18)[0] == 0xabcd6543, 'unsupported inner magic')
    require(struct.unpack_from('<I', data, HEADER_CRC)[0] == crc(data[0x22:HEADER_CRC]), 'compression table CRC32')
    require(struct.unpack_from('<I', data, len(data) - 4)[0] == crc(data[STREAM_START:-4]), 'compressed stream CRC32')
    meta_size, meta_packed = struct.unpack_from('<II', data, 0x22)
    require(meta_size == 344, 'unsupported partition metadata size')
    entries = [(meta_size, meta_packed, None, None)]
    entries += [struct.unpack_from('<IIII', data, TABLE_START + i * 16) for i in range(COMPONENTS)]
    raws, offset = [], STREAM_START
    for index, (size, packed_size, start, end) in enumerate(entries):
        require(packed_size >= 13 and offset + packed_size <= len(data) - 4, f'stream {index} out of bounds')
        if index:
            require(start < end and 32 <= size <= end - start, f'component {index} exceeds its flash range')
        packed = data[offset:offset + packed_size]
        require(packed[:5] == bytes.fromhex('5d00800000'), f'stream {index} LZMA profile')
        require(struct.unpack_from('<Q', packed, 5)[0] == size, f'stream {index} LZMA length')
        # Raw LZMA1 with the header's own properties: older liblzma builds reject .lzma streams that carry
        # both the size field and an end marker, as these do.
        props, dict_size = packed[0], int.from_bytes(packed[1:5], 'little')
        require(props < 225, f'stream {index} LZMA properties')
        pb, rest = divmod(props, 45)
        lp, lc = divmod(rest, 9)
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
            {'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}])
        try:
            raw = decoder.decompress(packed[13:], max_length=size)
        except lzma.LZMAError as e:
            raise FlashError(f'Bundled MemoMind firmware stream {index} does not decompress: {e}') from e
        # The declared size terminates each stream (most carry no end marker); the container's CRC32s and the
        # pinned image hash cover integrity.
        require(len(raw) == size and not decoder.unused_data, f'stream {index} end boundary')
        if index:
            words = struct.unpack_from('<8I', raw)
            require(words[0] == 0xa4e49a17 and words[1] == len(raw) - 32, f'component {index} header')
            require(words[4] == crc(raw[32:]), f'component {index} body CRC32')
        raws.append((raw, start, end))
        offset += packed_size
    require(offset == len(data) - 4, 'unaccounted bytes before the trailer')
    metadata = raws[0][0]
    require(struct.unpack_from('<6I', metadata)[1] == 16, 'unsupported partition count')
    partitions = [struct.unpack_from('<5I', metadata, 24 + 20 * i) for i in range(16)]
    for index, (raw, start, end) in enumerate(raws[1:], 1):
        matches = [p for p in partitions if p[1] == start]
        require(len(matches) == 1, f'component {index} has no unique partition')
        _, flash, load, body_size, allocation = matches[0]
        require(flash + allocation == end and body_size == len(raw) - 32
                and load == struct.unpack_from('<I', raw, 12)[0], f'component {index} disagrees with the partitions')


def load_cfw():
    data = paths.firmware(CFW)
    if len(data) != CFW_SIZE or hashlib.sha256(data).hexdigest() != CFW_SHA256:
        raise FlashError('Refusing an unreviewed MemoMind firmware image')
    check_container(data)
    return data
