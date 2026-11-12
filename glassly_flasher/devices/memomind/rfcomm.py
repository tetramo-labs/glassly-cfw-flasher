"""Bluetooth Classic RFCOMM to a paired MemoMind One: one channel interface over three OS backends.

macOS goes through native/memomind_rfcomm.m over IOBluetooth, the API the research tool's MacRFCOMMTransport made
the verified installs with: services found by their SDP record, all calls on the main thread while it pumps the
main run loop. The Windows (Winsock AF_BTH) and Linux (BlueZ RFCOMM socket) backends have not been run against
the glasses.

A channel has write(data), read(timeout) -> non-empty bytes or TimeoutError, drain() -> whatever arrived without
waiting, and close(). Every failure raises LinkError and leaves the channel failed; nothing is retried.
"""
import ctypes
from dataclasses import dataclass
import errno
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid

from ... import paths
from ..base import FlashError

SERVICES = {'normal': 0x7033, 'ota': 0x7034}
# What the glasses' SDP records gave the vendor app (research analysis/protocol/ota-final/rfcomm.tsv) and the Mac
# installs (captures/*/trace.jsonl). Linux falls back to them only when sdptool cannot be used.
RECORDED_CHANNELS = {0x7033: 5, 0x7034: 6}
NAME_PREFIX = 'memomind'
CONNECT_SECONDS = 20
WRITE_SECONDS = 10

AF_BTH = 32
BTHPROTO_RFCOMM = 3
LINUX_AF_BLUETOOTH = 31
LINUX_BTPROTO_RFCOMM = 3
WSAEWOULDBLOCK = 10035
ERROR_NO_MORE_ITEMS = 259


class LinkError(FlashError):
    pass


@dataclass(frozen=True)
class PairedDevice:
    address: str
    name: str


def normalize(address):
    text = address.strip().upper().replace('-', ':')
    if not re.fullmatch(r'[0-9A-F]{2}(:[0-9A-F]{2}){5}', text):
        raise LinkError(f'Not a Bluetooth address: {address!r}')
    return text


def describe(paired):
    return ', '.join(f'{d.name or "unnamed"} ({d.address})' for d in paired) or 'none'


def select_glasses(paired):
    """The one paired device named MemoMind..., None when there is none."""
    glasses = [d for d in paired if d.name.strip().lower().startswith(NAME_PREFIX)]
    if len(glasses) > 1:
        raise FlashError(f'More than one pair of MemoMind glasses is paired with this computer: {describe(glasses)}. '
                         'Remove all but the pair to flash in Bluetooth settings. Nothing was written.')
    return glasses[0] if glasses else None


class Radio:
    def paired(self):
        raise NotImplementedError

    def connect(self, address, service):
        raise NotImplementedError

    def sleep(self, seconds):
        time.sleep(seconds)

    def monotonic(self):
        return time.monotonic()


class TracedChannel:
    """Writes every byte sent and received to the session trace (sent bytes before they go out)."""

    def __init__(self, channel, trace, session):
        self.channel = channel
        self.trace = trace
        self.session = session

    def record(self, event, data):
        self.trace.write(json.dumps({'t': round(time.monotonic(), 6), 'session': self.session, 'event': event,
                                     'hex': bytes(data).hex()}) + '\n')
        self.trace.flush()

    def write(self, data):
        self.record('tx', data)
        self.channel.write(data)

    def read(self, timeout):
        data = self.channel.read(timeout)
        self.record('rx', data)
        return data

    def drain(self):
        data = self.channel.drain()
        if data:
            self.record('rx', data)
        return data

    def close(self):
        self.channel.close()


# macOS

def parse_mac_paired(text):
    devices = []
    for line in text.splitlines():
        address, _, name = line.partition('\t')
        if address:
            devices.append(PairedDevice(normalize(address), name.strip()))
    return devices


class MacChannel:
    def __init__(self, radio, handle):
        self.radio = radio
        self.handle = handle
        self.buffer = ctypes.create_string_buffer(1 << 16)
        self.channel_id = radio.lib.mm_channel(handle)
        self.source = radio.lib.mm_source(handle).decode()

    def write(self, data):
        data = bytes(data)
        if self.radio.lib.mm_write(self.handle, data, len(data), WRITE_SECONDS * 1000) != 0:
            raise LinkError(f'Bluetooth write failed: {self.radio.error()}')

    def receive(self, milliseconds):
        count = self.radio.lib.mm_read(self.handle, self.buffer, len(self.buffer), milliseconds)
        if count < 0:
            raise LinkError(f'Bluetooth read failed: {self.radio.error()}')
        return self.buffer.raw[:count]

    def read(self, timeout):
        data = self.receive(max(1, int(timeout * 1000)))
        if not data:
            raise TimeoutError('No data from the glasses')
        return data

    def drain(self):
        return self.receive(0)

    def close(self):
        self.radio.lib.mm_close(self.handle)


class MacRadio(Radio):
    def __init__(self):
        try:
            lib = ctypes.CDLL(str(paths.resource('native/libmemomind_rfcomm.dylib')))
        except OSError as e:
            raise LinkError(f'Cannot load the Bluetooth helper: {e}') from e
        lib.mm_error.restype = ctypes.c_char_p
        lib.mm_source.restype = ctypes.c_char_p
        lib.mm_source.argtypes = [ctypes.c_int]
        lib.mm_paired.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.mm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.mm_channel.argtypes = [ctypes.c_int]
        lib.mm_write.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.mm_read.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.mm_close.argtypes = [ctypes.c_int]
        self.lib = lib

    def error(self):
        return (self.lib.mm_error() or b'').decode('utf-8', 'replace')

    def paired(self):
        buffer = ctypes.create_string_buffer(1 << 16)
        count = self.lib.mm_paired(buffer, len(buffer))
        if count < 0:
            raise LinkError(f'Cannot list paired Bluetooth devices: {self.error()}')
        return parse_mac_paired(buffer.raw[:count].decode('utf-8', 'replace'))

    def connect(self, address, service):
        handle = self.lib.mm_open(normalize(address).encode(), SERVICES[service], 0, CONNECT_SECONDS * 1000)
        if handle <= 0:
            raise LinkError(f'Could not open the glasses\' {service} Bluetooth service: {self.error()}')
        return MacChannel(self, handle)


# Windows and Linux: a socket connected through the C library, then used through Python's socket object

class SocketChannel:
    def __init__(self, sock, channel_id, source):
        self.sock = sock
        self.channel_id = channel_id
        self.source = source
        self.failure = None

    def fail(self, reason):
        if self.failure is None:
            self.failure = reason
        try:
            self.sock.close()
        except OSError:
            pass
        return LinkError(self.failure)

    def check(self):
        if self.failure is not None:
            raise LinkError(self.failure)

    def write(self, data):
        self.check()
        try:
            self.sock.settimeout(WRITE_SECONDS)
            self.sock.sendall(bytes(data))
        except OSError as e:
            raise self.fail(f'Bluetooth write failed: {e}') from e

    def read(self, timeout):
        self.check()
        try:
            self.sock.settimeout(max(timeout, 0.001))
            data = self.sock.recv(1 << 16)
        except TimeoutError:
            raise TimeoutError('No data from the glasses') from None
        except OSError as e:
            raise self.fail(f'Bluetooth read failed: {e}') from e
        if not data:
            raise self.fail('The glasses closed the Bluetooth connection')
        return data

    def drain(self):
        self.check()
        try:
            ready = select.select([self.sock], [], [], 0)[0]
        except (OSError, ValueError) as e:
            raise self.fail(f'Bluetooth read failed: {e}') from e
        return self.read(0.001) if ready else b''

    def close(self):
        if self.failure is None:
            self.failure = 'The Bluetooth channel was closed'
        try:
            self.sock.close()
        except OSError:
            pass


def connect_raw(sock, address, timeout, call, last_error, in_progress):
    """Non-blocking connect() through the C library, for address formats Python's socket module cannot build."""
    sock.setblocking(False)
    if call(sock.fileno(), address, len(address)) != 0:
        code = last_error()
        if code not in in_progress:
            raise OSError(code, os.strerror(code) if code < 1000 else f'Winsock error {code}')
        _, writable, failed = select.select([], [sock], [sock], timeout)
        if not writable and not failed:
            raise TimeoutError('Bluetooth connection timed out')
        code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if code or failed:
            raise OSError(code, os.strerror(code) if code < 1000 else f'Winsock error {code}')
    sock.setblocking(True)


class Guid(ctypes.Structure):
    _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16), ('data3', ctypes.c_uint16),
                ('data4', ctypes.c_uint8 * 8)]


class SockaddrBth(ctypes.Structure):
    _pack_ = 1
    _layout_ = 'ms'
    _fields_ = [('family', ctypes.c_uint16), ('address', ctypes.c_uint64), ('service', Guid), ('port', ctypes.c_uint32)]


class SystemTime(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint16) for name in ('year', 'month', 'weekday', 'day', 'hour', 'minute', 'second',
                                                      'milliseconds')]


class BluetoothDeviceInfo(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('address', ctypes.c_uint64), ('device_class', ctypes.c_uint32),
                ('connected', ctypes.c_int32), ('remembered', ctypes.c_int32), ('authenticated', ctypes.c_int32),
                ('last_seen', SystemTime), ('last_used', SystemTime), ('name', ctypes.c_uint16 * 248)]


class BluetoothSearchParams(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('authenticated', ctypes.c_int32), ('remembered', ctypes.c_int32),
                ('unknown', ctypes.c_int32), ('connected', ctypes.c_int32), ('inquiry', ctypes.c_int32),
                ('timeout', ctypes.c_uint8), ('radio', ctypes.c_void_p)]


def format_address(value):
    return ':'.join(f'{(value >> shift) & 0xff:02X}' for shift in range(40, -8, -8))


def sockaddr_bth(address, short):
    """Port 0 with the service class GUID: Windows resolves the RFCOMM channel by SDP."""
    service = Guid.from_buffer_copy(uuid.UUID(f'0000{short:04x}-0000-1000-8000-00805f9b34fb').bytes_le)
    return bytes(SockaddrBth(AF_BTH, int(normalize(address).replace(':', ''), 16), service, 0))


class WindowsRadio(Radio):
    """Hardware-untested: never run against the glasses."""

    def paired(self):
        try:
            api = ctypes.WinDLL('BluetoothAPIs.dll', use_last_error=True)
        except OSError as e:
            raise LinkError(f'Bluetooth is not available on this PC ({e}).') from e
        api.BluetoothFindFirstDevice.restype = ctypes.c_void_p
        api.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(BluetoothSearchParams),
                                                 ctypes.POINTER(BluetoothDeviceInfo)]
        api.BluetoothFindNextDevice.restype = ctypes.c_int32
        api.BluetoothFindNextDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(BluetoothDeviceInfo)]
        api.BluetoothFindDeviceClose.argtypes = [ctypes.c_void_p]
        search = BluetoothSearchParams(ctypes.sizeof(BluetoothSearchParams), 1, 1, 0, 0, 0, 0, None)
        info = BluetoothDeviceInfo(ctypes.sizeof(BluetoothDeviceInfo))
        found = api.BluetoothFindFirstDevice(ctypes.byref(search), ctypes.byref(info))
        if not found:
            code = ctypes.get_last_error()
            if code == ERROR_NO_MORE_ITEMS:
                return []
            raise LinkError(f'Windows could not list paired Bluetooth devices (error {code}). Is Bluetooth on?')
        devices = []
        try:
            while True:
                if info.authenticated or info.remembered:
                    name = bytes(info.name).decode('utf-16-le', 'replace').split('\0', 1)[0]
                    devices.append(PairedDevice(format_address(info.address), name.strip()))
                info.size = ctypes.sizeof(BluetoothDeviceInfo)
                if not api.BluetoothFindNextDevice(found, ctypes.byref(info)):
                    break
        finally:
            api.BluetoothFindDeviceClose(found)
        return devices

    def connect(self, address, service):
        ws2 = ctypes.WinDLL('ws2_32', use_last_error=True)
        ws2.connect.argtypes = [ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int]
        try:
            sock = socket.socket(AF_BTH, socket.SOCK_STREAM, BTHPROTO_RFCOMM)
        except OSError as e:
            raise LinkError(f'Bluetooth is not available on this PC ({e}).') from e
        try:
            connect_raw(sock, sockaddr_bth(address, SERVICES[service]), CONNECT_SECONDS, ws2.connect,
                        ws2.WSAGetLastError, {WSAEWOULDBLOCK})
        except OSError as e:
            sock.close()
            raise LinkError(f'Could not open the glasses\' {service} Bluetooth service: {e}') from e
        return SocketChannel(sock, None, 'windows-sdp')


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[A-Za-z]|\x01|\x02', '', text)


def parse_bluetoothctl(text):
    devices = []
    for line in strip_ansi(text).splitlines():
        match = re.search(r'\bDevice ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}) ?(.*)$', line)
        if match:
            devices.append(PairedDevice(normalize(match.group(1)), match.group(2).strip()))
    return devices


def parse_sdptool(text, short):
    """The RFCOMM channel of the record that lists the service, None when no record lists it."""
    pattern = re.compile(rf'(?i)\b0x{short:04x}\b|0000{short:04x}-0000-1000-8000-00805f9b34fb')
    lines = [line for line in text.splitlines() if not line.startswith('Searching for')]
    channels = set()
    for record in re.split(r'\n\s*\n', '\n'.join(lines)):
        channel = re.search(r'Channel: (\d+)', record)
        if pattern.search(record) and channel:
            channels.add(int(channel.group(1)))
    if len(channels) > 1:
        raise LinkError(f'The glasses list service {short:04x} on several RFCOMM channels: {sorted(channels)}')
    return channels.pop() if channels else None


def sockaddr_rc(address, channel):
    return struct.pack('<H6sBx', LINUX_AF_BLUETOOTH, bytes.fromhex(normalize(address).replace(':', ''))[::-1], channel)


class LinuxRadio(Radio):
    """Hardware-untested: never run against the glasses. Needs BlueZ (bluetoothctl); no root."""

    def run(self, *command, timeout=15):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise LinkError(f'{command[0]} was not found. Install BlueZ (the bluez package).') from e
        except subprocess.TimeoutExpired as e:
            raise LinkError(f'{command[0]} did not answer; is the Bluetooth service (bluetoothd) running?') from e

    def paired(self):
        result = self.run('bluetoothctl', 'paired-devices')
        if 'Invalid command' in result.stdout + result.stderr:
            result = self.run('bluetoothctl', 'devices', 'Paired')
        return parse_bluetoothctl(result.stdout)

    def channel(self, address, short):
        if shutil.which('sdptool') is None:
            return RECORDED_CHANNELS[short], 'recorded channel (sdptool not installed)'
        result = self.run('sdptool', 'search', '--bdaddr', address, f'0x{short:04x}', timeout=30)
        channel = parse_sdptool(result.stdout, short)
        if channel is not None:
            return channel, 'sdptool'
        if result.returncode == 0 and 'Failed' not in result.stdout + result.stderr:
            raise LinkError(f'The glasses do not list Bluetooth service {short:04x}.')
        return RECORDED_CHANNELS[short], f'recorded channel (sdptool: {(result.stderr or result.stdout).strip()[:120]})'

    def connect(self, address, service):
        address = normalize(address)
        channel, source = self.channel(address, SERVICES[service])
        try:
            sock = socket.socket(LINUX_AF_BLUETOOTH, socket.SOCK_STREAM, LINUX_BTPROTO_RFCOMM)
        except OSError as e:
            raise LinkError(f'Bluetooth is not available on this computer ({e}).') from e
        try:
            if hasattr(socket, 'AF_BLUETOOTH'):
                sock.settimeout(CONNECT_SECONDS)
                sock.connect((address, channel))
            else:
                libc = ctypes.CDLL(None, use_errno=True)
                libc.connect.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                connect_raw(sock, sockaddr_rc(address, channel), CONNECT_SECONDS, libc.connect, ctypes.get_errno,
                            {errno.EINPROGRESS})
        except OSError as e:
            sock.close()
            raise LinkError(f'Could not open the glasses\' {service} Bluetooth service on channel {channel}: '
                            f'{e}') from e
        return SocketChannel(sock, channel, source)


def open_radio():
    if sys.platform == 'darwin':
        return MacRadio()
    if sys.platform == 'win32':
        return WindowsRadio()
    if sys.platform.startswith('linux'):
        return LinuxRadio()
    raise FlashError(f'Unsupported platform: {sys.platform}')
