"""MemoMind One GM control framing, the identity gate and the stock OTA updater transaction.

Ported from glassly-memomind-one-cfw at e56172a3: swift/MemoMindKit/Sources/MemoMindCore/Protocol.swift,
Session.swift and OTATransaction.swift (the code `memomind install` flashed revision 25 with on 2026-11-11), with
the GM stream parser from scripts/memomind_transport.py. The transaction never resends anything: any unexpected
reply, timeout or link failure ends it.
"""
from dataclasses import dataclass
import json
import time
import zlib

from ..base import FlashError

MAX_GM_LOGICAL = 80 * 1024
GM_REQUEST_BODY = 642
OTA_MAGIC = b'\x70\x07\x6e'
FILE_HEADER = 18
BLOCK_MAX = 4096
FRAGMENT = 630
PACING_FIELD = 2000

MODEL = 'H001Y'
HARDWARE = '2.1.2'
FRONT = '1.3'
# Only 1.0.3.127 has ever been flashed to a Glassly revision; 1.0.2.151 was only updated by the vendor's own app.
SOURCE_VERSIONS = ('1.0.3.127',)


class ProtocolError(FlashError):
    pass


def require(condition, reason):
    if not condition:
        raise ProtocolError(reason)


@dataclass(frozen=True)
class GmMessage:
    head: int
    event: int
    service: int
    command: int
    body: bytes
    tlvs: list

    def matches(self, event, service, command):
        return (self.event, self.service, self.command) == (event, service, command)


def gm_frame(event, service, command, body=b''):
    """One GM request packet. Every request this flasher sends fits the glasses' 642-byte ingress fragment."""
    body = bytes(body)
    require(len(body) <= GM_REQUEST_BODY, 'GM request needs fragmentation, which this flasher does not send')
    raw = b'\xfa' + (len(body) + 9).to_bytes(3, 'big') + bytes([event, service, command]) + body
    return raw + (sum(raw) & 0xffff).to_bytes(2, 'big')


def parse_tlvs(body):
    result, offset = [], 0
    while offset < len(body):
        require(len(body) - offset >= 4, 'Truncated GM TLV header')
        kind, length = body[offset], int.from_bytes(body[offset + 1:offset + 4], 'big')
        offset += 4
        require(length <= len(body) - offset, 'Truncated GM TLV value')
        value = bytes(body[offset:offset + length])
        offset += length
        fixed = {3: 4, 6: 1, 7: 2, 8: 1}.get(kind)
        require(fixed is None or len(value) == fixed, 'Invalid fixed-width GM TLV')
        result.append((kind, value))
    return result


class GmStream:
    """Reply-side GM stream: 503-byte fragments, the glasses' repeated-FA continuation heads and their empty
    per-fragment acknowledgments."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.buffer = bytearray()
        self.body = bytearray()
        self.key = None
        self.fragment = 0

    def feed(self, data):
        self.buffer.extend(data)
        messages = []
        try:
            while len(self.buffer) >= 7:
                head = self.buffer[0]
                logical = int.from_bytes(self.buffer[1:4], 'big')
                key = (logical, self.buffer[4], self.buffer[5], self.buffer[6])
                require(9 <= logical <= MAX_GM_LOGICAL, 'Invalid GM logical length')
                if logical == 9 and (head == 0xfa or 1 <= head < 0xf0):
                    if len(self.buffer) < 9:
                        break
                    packet = bytes(self.buffer[:9])
                    require(sum(packet[:-2]) & 0xffff == int.from_bytes(packet[-2:], 'big'),
                            'GM additive checksum mismatch')
                    del self.buffer[:9]
                    messages.append(GmMessage(head, key[1], key[2], key[3], b'', []))
                    continue
                repeated = self.key == key and head == 0xfa
                first = head == 0xfa and not repeated
                if not first:
                    require(self.key == key and (head == self.fragment or repeated),
                            f'GM continuation identity or order mismatch at {bytes(self.buffer[:16]).hex()}')
                physical = min(512, logical - (0 if first else len(self.body)))
                require(physical >= 9, 'Invalid GM continuation length')
                if len(self.buffer) < physical:
                    break
                packet = bytes(self.buffer[:physical])
                require(sum(packet[:-2]) & 0xffff == int.from_bytes(packet[-2:], 'big'),
                        'GM additive checksum mismatch')
                del self.buffer[:physical]
                if first:
                    self.key = key
                    self.body.clear()
                    self.fragment = 0
                self.body.extend(packet[7:-2])
                self.fragment += 1
                if len(self.body) == logical - 9:
                    body = bytes(self.body)
                    messages.append(GmMessage(head, key[1], key[2], key[3], body, parse_tlvs(body)))
                    self.body.clear()
                    self.key = None
                    self.fragment = 0
        except Exception:
            self.reset()
            raise
        return messages


class GmSession:
    """One normal-service (0x7033) connection. Requests are sent one at a time and never resent."""

    def __init__(self, channel, timeout=15.0):
        self.channel = channel
        self.timeout = timeout
        self.parser = GmStream()
        self.unsolicited = []

    def request(self, event, service, command, body=b''):
        self.channel.write(gm_frame(event, service, command, body))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'No reply to GM {service}/{command:#04x}')
            matched = None
            for message in self.parser.feed(self.channel.read(remaining)):
                if message.matches(event, service, command):
                    if message.tlvs:
                        matched = message
                else:
                    self.unsolicited.append(message)
            if matched is not None:
                return matched

    def probe(self):
        """Heartbeat and device information, the research tool's read-only probe. Returns the device-info JSON."""
        heartbeat = self.request(1, 1, 1)
        require([v for k, v in heartbeat.tlvs if k == 6] == [b'\x00'], 'Heartbeat did not return STATUS 0')
        info = self.request(2, 1, 2)
        strings = [v for k, v in info.tlvs if k == 2]
        require(len(strings) == 1, 'Device-info reply did not contain exactly one STRING')
        try:
            device = json.loads(strings[0].rstrip(b'\0'))
        except ValueError as e:
            raise ProtocolError(f'Device-info reply is not JSON: {strings[0][:200]!r}') from e
        require(isinstance(device, dict), 'Device-info reply is not a JSON object')
        return device

    def capabilities(self):
        return self.request(3, 15, 0x7e).tlvs

    def notify_ota(self):
        reply = self.request(0x15, 1, 0x1c, bytes([8, 0, 0, 1, 1]))
        require(reply.tlvs == [(6, b'\x01')], f'Unsupported pre-OTA notification reply {reply.tlvs!r}')


def describe_identity(device):
    return (f'model {device.get("model")!r}, hardware {device.get("hver")!r}, front {device.get("fver")!r}, '
            f'firmware {device.get("ver")!r}, MAC {device.get("mac")!r}')


def is_known_kind(device):
    return device.get('model') == MODEL and device.get('hver') == HARDWARE and device.get('fver') == FRONT


def check_identity(device, address):
    """The research tool's validate_identity, unchanged: the only glasses its OTA path has written to."""
    mac = device.get('mac')
    if (not is_known_kind(device) or device.get('ver') not in SOURCE_VERSIONS
            or not isinstance(mac, str) or mac.upper() != address.upper()):
        raise FlashError(f'These glasses reported {describe_identity(device)} (paired address {address}). The flasher '
                         f'only writes to model {MODEL}, hardware {HARDWARE}, front {FRONT} on firmware '
                         f'{" or ".join(SOURCE_VERSIONS)}, whose MAC is the paired address. Nothing was written.')


def installed_revision(tlvs):
    """None for official firmware, else the Glassly CFW revision number from the capabilities reply."""
    if tlvs == [(6, b'\x02')]:
        return None
    documents = [v for k, v in tlvs if k == 5]
    version = None
    if len(documents) == 1:
        try:
            version = json.loads(documents[0].rstrip(b'\0')).get('version')
        except (ValueError, AttributeError):
            version = None
    prefix, _, number = version.partition('/') if isinstance(version, str) else ('', '', '')
    if prefix != 'GLASSLYCFW' or not number.isdigit() or not number.isascii():
        raise FlashError(f'The glasses answered the capabilities query with {tlvs!r:.300}, which is neither the '
                         'official firmware nor a Glassly firmware revision. Nothing was written.')
    return int(number)


def encode_ota(command, sequence, parameters=b'', flags=0xc0):
    payload = bytes([sequence]) + bytes(parameters)
    require(len(payload) <= 0xffff, 'OTA payload exceeds the wire length field')
    return OTA_MAGIC + bytes([flags, command]) + len(payload).to_bytes(2, 'big') + payload + b'\x33'


def encode_chunk(sequence, offset, data):
    require(len(data) <= FRAGMENT, 'OTA chunk longer than the observed format')
    return encode_ota(0xe5, sequence, offset.to_bytes(4, 'big') + data + zlib.crc32(data).to_bytes(4, 'big'))


class OtaStream:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 7:
            require(self.buffer[:3] == OTA_MAGIC, 'Unrecognised bytes in the OTA stream')
            total = int.from_bytes(self.buffer[5:7], 'big') + 8
            if len(self.buffer) < total:
                break
            require(self.buffer[total - 1] == 0x33, 'Invalid OTA frame footer')
            frames.append(bytes(self.buffer[:total]))
            del self.buffer[:total]
        return frames


class OtaTransaction:
    """The stock updater exchange on the OTA service (0x7034). Single use; the normal connection stays open and is
    drained through `check_control` before every write and wait."""

    def __init__(self, channel, image, device, address, check_control, sleep, pace=0.045, timeout=15.0,
                 progress=None, log=None):
        check_identity(device, address)
        require(FILE_HEADER < len(image) <= 0xffffffff, 'Invalid image length')
        self.channel = channel
        self.image = image
        self.check_control = check_control
        self.sleep = sleep
        self.pace = pace
        self.timeout = timeout
        self.progress = progress or (lambda done, total: None)
        self.log = log or (lambda text: None)
        self.parser = OtaStream()
        self.pending = []
        self.sequence = 0
        self.started = False
        self.phase = 'validated'
        self.blocks = 0
        self.chunks = 0
        self.paced = 0

    def next_sequence(self):
        self.sequence = (self.sequence + 1) & 0xff
        return self.sequence

    def write(self, data):
        self.check_control()
        self.channel.write(data)

    def response(self, command, sequence):
        deadline = time.monotonic() + self.timeout
        while not self.pending:
            self.check_control()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('OTA reply timeout')
            self.pending.extend(self.parser.feed(self.channel.read(remaining)))
        frame = self.pending.pop(0)
        require(frame[3] == 0 and frame[4] == command, 'Unexpected OTA reply flags/command')
        payload = frame[7:-1]
        require(len(payload) >= 2 and payload[0] == 0 and payload[1] == sequence, 'Unexpected OTA status/sequence')
        require(not self.pending and not self.parser.buffer, 'Unexpected additional OTA reply bytes')
        return payload[2:]

    def command(self, command, parameters=b''):
        self.phase = f'command {command:#04x}'
        sequence = self.next_sequence()
        self.write(encode_ota(command, sequence, parameters))
        return self.response(command, sequence)

    def run(self):
        require(not self.started, 'OTA transaction is single use')
        self.started = True
        try:
            return self.execute()
        except (FlashError, TimeoutError, OSError) as e:
            raise FlashError(f'Stopped during {self.phase}: {e}. Nothing was resent.') from e

    def execute(self):
        attached = self.command(0x02, b'\xff' * 5)
        fields = {}
        while attached:
            length = attached[0]
            require(length >= 1 and len(attached) >= length + 1, 'Malformed attach TLV')
            require(attached[1] not in fields, 'Duplicate attach TLV')
            fields[attached[1]] = attached[2:length + 1]
            attached = attached[length + 1:]
        require(fields.get(1) == self.image[:4], 'OTA attach profile differs from the image')
        require(self.command(0xe1) == bytes.fromhex('000000000012'), 'Unsupported OTA file-identification request')
        require(self.command(0xe2, self.image[:FILE_HEADER]) == b'\x03',
                'The glasses did not return can-update result 3; the update was not entered')
        entry = self.command(0xe3)
        require(len(entry) == 8 and entry[0] == 0 and entry[-1] == 1, 'Unsupported OTA entry result/CRC mode')
        offset, size = int.from_bytes(entry[1:5], 'big'), int.from_bytes(entry[5:7], 'big')
        # The glasses keep a cursor only while e2's payload length and CRC32 match the image they were staging, and
        # verify the whole stage before committing (research analysis/ghidra/findings.md, "Initial OTA offset and
        # resume"; continued on hardware in captures/v18-flash-20261108-08).
        require(FILE_HEADER <= offset < len(self.image),
                f'The glasses asked to start at byte {offset} of {len(self.image)}; refusing before any firmware byte')
        resumed = offset if offset != FILE_HEADER else None
        if resumed is not None:
            self.log(f'The glasses hold an earlier, interrupted transfer of this image; continuing from byte {offset}.')
        first, previous_end = offset, None
        while True:
            self.phase = f'block at offset {offset}, length {size}'
            require(0 < size <= BLOCK_MAX and offset < len(self.image) and size <= len(self.image) - offset,
                    'Requested block out of bounds')
            require(previous_end is None or offset == previous_end, 'Noncontiguous or repeated block request')
            base = self.next_sequence()
            sequence = base
            for index, relative in enumerate(range(0, size, FRAGMENT)):
                sequence = (base + index) & 0xff
                fragment = self.image[offset + relative:offset + min(relative + FRAGMENT, size)]
                self.write(encode_chunk(sequence, offset + relative, fragment))
                self.chunks += 1
                if relative + FRAGMENT < size and self.pace:
                    self.sleep(self.pace)
            reply = self.response(0xe5, sequence)
            require(len(reply) == 9 and reply[0] == 0, 'Unsupported OTA block result')
            pacing = int.from_bytes(reply[7:9], 'big')
            require(pacing in (0, PACING_FIELD), f'Unsupported OTA pacing field {pacing}')
            self.blocks += 1
            previous_end = offset + size
            self.progress(previous_end, len(self.image))
            offset, size = int.from_bytes(reply[1:5], 'big'), int.from_bytes(reply[5:7], 'big')
            terminal = offset == 0 and size == 0
            if terminal:
                require(previous_end == len(self.image), 'Terminal acknowledgment before the end of the image')
            else:
                require(offset == previous_end and 0 < size <= BLOCK_MAX and offset < len(self.image)
                        and size <= len(self.image) - offset, 'Invalid next OTA block range')
            if pacing:
                # Stock asks for 2000 while it is busy (research analysis/ota-pacing-20261103/NOTES.md).
                self.phase = f'device pacing at offset {offset}'
                self.paced += 1
                for _ in range(20):
                    self.check_control()
                    self.sleep(0.1)
                self.check_control()
            if terminal:
                break
        require(self.command(0xe6) == b'\x00', 'Unsupported update-status result')
        require(self.command(0x03, b'\x00') == b'\x00', 'Unsupported reboot result')
        self.phase = 'reboot acknowledged'
        return {'state': self.phase, 'blocks': self.blocks, 'chunks': self.chunks,
                'transferred_range': [first, previous_end], 'resumed_from': resumed, 'paced_acks': self.paced,
                'attach_fields': {str(k): v.hex() for k, v in sorted(fields.items())}}
