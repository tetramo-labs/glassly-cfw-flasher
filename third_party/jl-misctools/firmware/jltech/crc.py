""" CRC calculation routines """

__all__ = [
    'jl_crc16',
]

import binascii


def jl_crc16(data, crc=0x0000):
    return binascii.crc_hqx(data, crc)
