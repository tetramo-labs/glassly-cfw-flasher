"""MemoMind One install and backup against scripted glasses, plus byte-for-byte replays of real exchanges.
No Bluetooth access.

The replays use the research repository's local captures and the revision 25 install trace in
~/Library/Logs/Glassly, and skip when those are absent."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zlib

from glassly_flasher.devices import DEVICES
from glassly_flasher.devices import memomind
from glassly_flasher.devices.base import Cancelled, FlashError, Reporter
from glassly_flasher.devices.memomind import Session, rfcomm, run
from glassly_flasher.devices.memomind.image import CFW_SHA256, check_container, load_cfw
from glassly_flasher.devices.memomind.protocol import (OTA_MAGIC, GmSession, GmStream, OtaStream, OtaTransaction,
                                                       ProtocolError, check_identity, encode_ota, gm_frame,
                                                       installed_revision)
from glassly_flasher.devices.memomind.rfcomm import LinkError, PairedDevice

RESEARCH = Path('/Users/fosse/dev/glassly-memomind-one-cfw')
INSTALL_25 = RESEARCH / 'captures/mac-install-25-20261111/trace.jsonl'
INSTALL_25_SHA = 'd42cba52a0989b4ad0e048edd214e1611f1aa8274654a5e7f83c2e303934271c'
RESUME_18 = RESEARCH / 'captures/v18-flash-20261108-08/trace.jsonl'
IMAGE_18 = RESEARCH / 'analysis/ble-probe-20261108b/image-18-01/firmware.bin'
VENDOR_TSV = RESEARCH / 'analysis/protocol/ota-final/rfcomm.tsv'
STOCK = RESEARCH / 'captures/firmware/ota-1.0.3.127-jhv3_release.bin'

IDENTITY = {'pver': 4, 'cap': 1, 'model': 'H001Y', 'sn': 'TEST00000001', 'mac': 'AA:BB:CC:DD:EE:01',
            'name': 'MemoMind One', 'ver': '1.0.3.127', 'cver': '0.0.0', 'hver': '2.1.2', 'fver': '1.3'}
ADDRESS = IDENTITY['mac']


def tlv_bytes(tlvs):
    return b''.join(bytes([kind]) + len(value).to_bytes(3, 'big') + value for kind, value in tlvs)


def gm_reply(event, service, command, tlvs):
    """A reply as the glasses send it: 503-byte body fragments, each one headed FA."""
    body = tlv_bytes(tlvs)
    logical = len(body) + 9
    out = b''
    for offset in range(0, max(1, len(body)), 503):
        raw = b'\xfa' + logical.to_bytes(3, 'big') + bytes([event, service, command]) + body[offset:offset + 503]
        out += raw + (sum(raw) & 0xffff).to_bytes(2, 'big')
    return out


def ota_reply(command, sequence, data=b''):
    payload = bytes([0, sequence]) + data
    return OTA_MAGIC + bytes([0, command]) + len(payload).to_bytes(2, 'big') + payload + b'\x33'


def capabilities_tlvs(revision):
    document = {'glassly': 'graphics-runtime', 'profile': 'H001Y-1.0.3.127', 'version': f'GLASSLYCFW/{revision}',
                'padding': 'x' * 1500}
    return [(5, json.dumps(document).encode() + b'\0')]


class FakeGlasses:
    """H001Y control and OTA services: stages e5 chunks, keeps a resume cursor keyed on the e2 file header, and comes
    back as revision 25 after a reboot when the stage equals the bundled image."""

    def __init__(self, image, revision=None, identity=None, capabilities=None, cursor=0, entry=None,
                 pacing_after=None, pacing_value=2000, bad_ack_at=None, boot_unreachable=0):
        self.image = image
        self.revision = revision
        self.identity = dict(identity or IDENTITY)
        self.capabilities = capabilities
        self.cursor = cursor
        self.stage = bytearray(image[18:18 + cursor])
        self.header = image[:18] if cursor else None
        self.entry = entry
        self.pacing_after = pacing_after
        self.pacing_value = pacing_value
        self.bad_ack_at = bad_ack_at
        self.boot_unreachable = boot_unreachable
        self.events = []
        self.notified = False
        self.rebooted = False
        self.blocks = 0
        self.chunk_offsets = []

    def gm(self, frame):
        assert frame[0] == 0xfa and sum(frame[:-2]) & 0xffff == int.from_bytes(frame[-2:], 'big')
        event, service, command = frame[4], frame[5], frame[6]
        self.events.append(('gm', service, command))
        if (service, command) == (1, 1):
            return gm_reply(event, 1, 1, [(6, b'\x00')])
        if (service, command) == (1, 2):
            return (gm_reply(6, 1, 0x30, [(8, b'\x01')]) +
                    gm_reply(event, 1, 2, [(2, json.dumps(self.identity).encode() + b'\0')]))
        if (service, command) == (15, 0x7e):
            if self.capabilities is not None:
                tlvs = self.capabilities
            else:
                tlvs = [(6, b'\x02')] if self.revision is None else capabilities_tlvs(self.revision)
            return gm_reply(event, 15, 0x7e, tlvs)
        if (service, command) == (1, 0x1c):
            assert frame[7:-2] == bytes([8, 0, 0, 1, 1])
            self.notified = True
            return gm_reply(event, 1, 0x1c, [(6, b'\x01')])
        raise AssertionError(f'Unexpected GM request {frame.hex()}')

    def ota(self, frame):
        assert self.notified and frame[3] == 0xc0
        command, sequence, params = frame[4], frame[7], frame[8:-1]
        self.events.append(('ota', command))
        if command == 0x02:
            fields = [(0, bytes.fromhex('007f000002')), (1, self.image[:4]), (2, bytes.fromhex('5364')),
                      (3, b'\x00'), (4, b'\x01'), (5, b'\x01')]
            return ota_reply(2, sequence, b''.join(bytes([len(v) + 1, k]) + v for k, v in fields))
        if command == 0xe1:
            return ota_reply(0xe1, sequence, bytes.fromhex('000000000012'))
        if command == 0xe2:
            if self.header != params[:18]:
                self.cursor = 0
                self.stage.clear()
            self.header = bytes(params[:18])
            return ota_reply(0xe2, sequence, b'\x03')
        if command == 0xe3:
            offset = 18 + self.cursor if self.entry is None else self.entry
            size = min(4096, max(len(self.image) - offset, 1))
            self.expected, self.block_end = offset, offset + size
            return ota_reply(0xe3, sequence, b'\x00' + offset.to_bytes(4, 'big') + size.to_bytes(2, 'big') + b'\x01')
        if command == 0xe5:
            offset, data = int.from_bytes(params[:4], 'big'), params[4:-4]
            assert zlib.crc32(data) == int.from_bytes(params[-4:], 'big') and offset == self.expected
            self.chunk_offsets.append(offset)
            self.stage += data
            self.expected += len(data)
            self.cursor = self.expected - 18
            if self.expected < self.block_end:
                return b''
            self.blocks += 1
            following = self.block_end
            size = min(4096, len(self.image) - following)
            if size == 0:
                following = 0
            self.block_end = following + size
            pacing = self.pacing_value if self.blocks == self.pacing_after else 0
            if self.blocks == self.bad_ack_at:
                sequence = (sequence + 1) & 0xff
            return ota_reply(0xe5, sequence, b'\x00' + following.to_bytes(4, 'big') + size.to_bytes(2, 'big') +
                             pacing.to_bytes(2, 'big'))
        if command == 0xe6:
            return ota_reply(0xe6, sequence, b'\x00' if bytes(self.stage) == self.image[18:] else b'\x01')
        if command == 0x03:
            assert params == b'\x00'
            self.rebooted = True
            if bytes(self.stage) == self.image[18:]:
                self.revision = 25
            return ota_reply(3, sequence, b'\x00')
        raise AssertionError(f'Unexpected OTA command {command:#04x}')


class FakeChannel:
    def __init__(self, radio, kind):
        self.radio = radio
        self.kind = kind
        self.inbox = bytearray()
        self.parser = OtaStream()
        self.closed = False

    def write(self, data):
        assert not self.closed
        self.radio.log.append(('write', self.kind, bytes(data)))
        if self.radio.fail_write and self.radio.fail_write(self.kind, bytes(data)):
            raise LinkError('Injected disconnect')
        if self.kind == 'normal':
            self.inbox += self.radio.glasses.gm(bytes(data))
        else:
            for frame in self.parser.feed(data):
                self.inbox += self.radio.glasses.ota(frame)

    def read(self, timeout):
        if not self.inbox:
            raise TimeoutError('fake glasses sent nothing')
        data = bytes(self.inbox[:self.radio.read_size])
        del self.inbox[:self.radio.read_size]
        return data

    def drain(self):
        self.radio.drains += 1
        if self.radio.fail_drain is not None and self.radio.drains >= self.radio.fail_drain:
            raise LinkError('Injected control link loss')
        data = bytes(self.inbox)
        self.inbox.clear()
        return data

    def close(self):
        self.closed = True


class FakeRadio(rfcomm.Radio):
    def __init__(self, glasses, paired=None):
        self.glasses = glasses
        self.paired_devices = [PairedDevice(ADDRESS, 'MemoMind One'), PairedDevice('11:22:33:44:55:66', 'Keyboard')] \
            if paired is None else paired
        self.log = []
        self.clock = 0.0
        self.read_size = 1 << 20
        self.fail_write = None
        self.fail_drain = None
        self.drains = 0
        self.refuse_connects = 0

    def paired(self):
        return list(self.paired_devices)

    def connect(self, address, service):
        assert address == ADDRESS
        self.log.append(('connect', service))
        if self.refuse_connects:
            self.refuse_connects -= 1
            raise LinkError('RFCOMM open timed out')
        if self.glasses.rebooted and self.glasses.boot_unreachable:
            self.glasses.boot_unreachable -= 1
            raise LinkError('RFCOMM open timed out')
        return FakeChannel(self, service)

    def sleep(self, seconds):
        self.log.append(('sleep', seconds))
        self.clock += seconds

    def monotonic(self):
        return self.clock

    def writes(self, kind=None):
        return [data for event, k, *rest in self.log if event == 'write' and kind in (None, k) for data in rest]

    def ota_commands(self):
        return [data[4] for data in self.writes('ota')]


class Recorder(Reporter):
    def __init__(self, cancel_after=None):
        self.lines = []
        self.progressed = []
        self.cancel_after = cancel_after
        self.polls = 0

    def log(self, text):
        self.lines.append(text)

    def status(self, text):
        self.lines.append(text)

    def progress(self, done, total):
        assert 0 <= done <= total
        self.progressed.append((done, total))

    def cancelled(self):
        self.polls += 1
        return self.cancel_after is not None and self.polls > self.cancel_after


def run_in(mode, radio, reporter=None):
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        result = run(mode, folder, reporter or Recorder(), link=radio)
        return result, {p.name: p.read_text() for p in folder.iterdir()}


def attempt(mode, radio, reporter=None):
    with tempfile.TemporaryDirectory() as folder:
        try:
            run(mode, Path(folder), reporter or Recorder(), link=radio)
        except FlashError as e:
            return e, sorted(p.name for p in Path(folder).iterdir())
    raise AssertionError('run() did not refuse')


class CodecTests(unittest.TestCase):
    def test_requests_match_the_revision_25_install(self):
        self.assertEqual(gm_frame(1, 1, 1).hex(), 'fa0000090101010106')
        self.assertEqual(gm_frame(2, 1, 2).hex(), 'fa0000090201020108')
        self.assertEqual(gm_frame(0x15, 1, 0x1c, bytes([8, 0, 0, 1, 1])).hex(), 'fa00000e15011c08000001010144')
        self.assertEqual(encode_ota(2, 1, b'\xff' * 5).hex(), '70076ec002000601ffffffffff33')
        self.assertEqual(encode_ota(0xe6, 0x29).hex(), '70076ec0e600012933')
        self.assertEqual(encode_ota(3, 0x2a, b'\x00').hex(), '70076ec00300022a0033')

    def test_repeated_fa_reply_survives_any_read_boundaries(self):
        tlvs = capabilities_tlvs(22)
        wire = gm_reply(9, 1, 0x30, [(8, b'\x01')]) + gm_reply(3, 15, 0x7e, tlvs) + gm_frame(4, 1, 2)
        for size in (1, 7, 500, len(wire)):
            parser, messages = GmStream(), []
            for start in range(0, len(wire), size):
                messages += parser.feed(wire[start:start + size])
            self.assertEqual([(m.event, m.service, m.command) for m in messages],
                             [(9, 1, 0x30), (3, 15, 0x7e), (4, 1, 2)])
            self.assertEqual(messages[1].tlvs, tlvs)
            self.assertEqual(messages[2].tlvs, [])

    def test_bad_checksum_is_refused(self):
        wire = bytearray(gm_reply(1, 1, 1, [(6, b'\x00')]))
        wire[-1] ^= 1
        with self.assertRaisesRegex(ProtocolError, 'checksum'):
            GmStream().feed(bytes(wire))

    def test_installed_revision(self):
        self.assertIsNone(installed_revision([(6, b'\x02')]))
        self.assertEqual(installed_revision(capabilities_tlvs(22)), 22)
        for tlvs in ([(6, b'\x01')], [(5, b'{"version":"GLASSLYCFW/x"}')], [(5, b'{}')], [(5, b'nope')], []):
            with self.assertRaisesRegex(FlashError, 'Nothing was written'):
                installed_revision(tlvs)


class IdentityTests(unittest.TestCase):
    def test_research_allowlist_exactly(self):
        check_identity(IDENTITY, ADDRESS.lower())
        for field, value in (('model', 'H002Y'), ('hver', '2.1.3'), ('fver', '1.4'), ('ver', '1.0.3.128'), ('ver', '1.0.2.151'),
                             ('ver', 'GLASSLYCFW/25'), ('mac', '00:11:22:33:44:55'), ('mac', None)):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(FlashError, f'{value!r}.*Nothing was written'):
                    check_identity({**IDENTITY, field: value}, ADDRESS)


class ImageTests(unittest.TestCase):
    def test_bundled_image_is_pinned_and_intact(self):
        image = load_cfw()
        self.assertEqual(hashlib.sha256(image).hexdigest(), CFW_SHA256)
        self.assertEqual(image[:4], b'ME\x02\x01')

    def test_damaged_container_is_refused(self):
        image = bytearray(load_cfw())
        image[100000] ^= 1
        with self.assertRaisesRegex(FlashError, 'CRC32'):
            check_container(bytes(image))

    def test_registry_offers_no_restore(self):
        self.assertEqual(DEVICES['memomind'].modes, ('install', 'backup'))
        self.assertEqual(DEVICES['memomind'].elevate, frozenset())
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FlashError, 'does not support restore'):
                run('restore', Path(folder), Recorder(), link=FakeRadio(None))


class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = load_cfw()

    def glasses(self, **kwargs):
        return FakeGlasses(self.image, **kwargs)

    def test_install_from_official_firmware(self):
        glasses = self.glasses()
        radio = FakeRadio(glasses)
        reporter = Recorder()
        result, files = run_in('install', radio, reporter)
        self.assertEqual(bytes(glasses.stage), self.image[18:])
        self.assertEqual((result['state'], result['booted_version'], result['installed_before']),
                         ('installed', 'GLASSLYCFW/25', 'official firmware 1.0.3.127'))
        self.assertEqual(result['transferred_range'], [18, len(self.image)])
        self.assertIsNone(result['resumed_from'])
        self.assertEqual(result['blocks'], -(-(len(self.image) - 18) // 4096))
        self.assertEqual(sorted(files), ['capabilities.json', 'device-info.json', 'trace.jsonl'])
        self.assertEqual(json.loads(files['capabilities.json'])['status'], 2)
        self.assertEqual(reporter.progressed[-1], (len(self.image), len(self.image)))
        connects = [e for e in radio.log if e[0] in ('connect', 'sleep') and e[1] != 0.045]
        self.assertEqual(connects, [('connect', 'normal'), ('sleep', 8), ('connect', 'normal'), ('connect', 'ota'),
                                    ('sleep', 8), ('connect', 'normal')])
        self.assertEqual(radio.ota_commands()[:4], [0x02, 0xe1, 0xe2, 0xe3])
        self.assertEqual(radio.ota_commands()[-2:], [0xe6, 0x03])
        trace = [json.loads(line) for line in files['trace.jsonl'].splitlines()]
        self.assertEqual({row['session'] for row in trace}, {'inspect', 'control', 'ota', 'boot-check'})

    def test_install_over_a_glassly_revision_it_was_installed_from(self):
        glasses = self.glasses(revision=22)
        result, _ = run_in('install', FakeRadio(glasses))
        self.assertEqual((result['installed_before'], glasses.revision), ('GLASSLYCFW/22', 25))

    def test_already_installed_writes_nothing(self):
        glasses = self.glasses(revision=25)
        radio = FakeRadio(glasses)
        result, files = run_in('install', radio)
        self.assertEqual((result['state'], result['flash_modified']), ('already installed', False))
        self.assertEqual(glasses.events, [('gm', 1, 1), ('gm', 1, 2), ('gm', 15, 0x7e)])
        self.assertNotIn(('connect', 'ota'), radio.log)

    def test_revisions_never_installed_over_are_refused(self):
        for revision in (2, 23, 24, 26):
            with self.subTest(revision=revision):
                glasses = self.glasses(revision=revision)
                radio = FakeRadio(glasses)
                error, files = attempt('install', radio)
                self.assertRegex(str(error), f'revision {revision}, .*Nothing was written')
                self.assertEqual(glasses.events, [('gm', 1, 1), ('gm', 1, 2), ('gm', 15, 0x7e)])
                self.assertEqual(files, ['capabilities.json', 'device-info.json', 'trace.jsonl'])

    def test_identity_refusals_send_nothing_but_reads(self):
        for field, value in (('model', 'H002Y'), ('hver', '2.2.0'), ('ver', '1.0.4.1'), ('mac', '00:11:22:33:44:55')):
            with self.subTest(field=field):
                glasses = self.glasses(identity={**IDENTITY, field: value})
                radio = FakeRadio(glasses)
                error, _ = attempt('install', radio)
                self.assertRegex(str(error), f'{value!r}.*Nothing was written')
                self.assertTrue(set(glasses.events) <= {('gm', 1, 1), ('gm', 1, 2), ('gm', 15, 0x7e)})
                self.assertNotIn(('connect', 'ota'), radio.log)

    def test_unrecognised_capabilities_are_refused(self):
        glasses = self.glasses(capabilities=[(6, b'\x01')])
        error, _ = attempt('install', FakeRadio(glasses))
        self.assertRegex(str(error), r"\(6, b'\\x01'\).*Nothing was written")
        self.assertFalse(glasses.notified)

    def test_interrupted_transfer_of_the_same_image_is_continued(self):
        cursor = 289 * 4096 + 5 * 630
        glasses = self.glasses(cursor=cursor)
        reporter = Recorder()
        result, _ = run_in('install', FakeRadio(glasses), reporter)
        self.assertEqual(result['resumed_from'], 18 + cursor)
        self.assertEqual(glasses.chunk_offsets[0], 18 + cursor)
        self.assertEqual(bytes(glasses.stage), self.image[18:])
        self.assertEqual(result['booted_version'], 'GLASSLYCFW/25')
        self.assertTrue(any('continuing from byte' in line for line in reporter.lines))

    def test_cursor_of_another_image_is_dropped_by_the_glasses(self):
        glasses = self.glasses(cursor=4096)
        glasses.header = b'ME\x02\x01' + bytes(14)
        result, _ = run_in('install', FakeRadio(glasses))
        self.assertIsNone(result['resumed_from'])
        self.assertEqual(glasses.chunk_offsets[0], 18)

    def test_entry_outside_the_image_is_refused_before_any_firmware_byte(self):
        for entry in (0, 17, len(self.image)):
            with self.subTest(entry=entry):
                radio = FakeRadio(self.glasses(entry=entry))
                error, _ = attempt('install', radio)
                self.assertRegex(str(error), f'start at byte {entry} .*No firmware bytes were sent')
                self.assertEqual(radio.ota_commands(), [0x02, 0xe1, 0xe2, 0xe3])

    def test_link_failure_mid_transfer_sends_nothing_more(self):
        radio = FakeRadio(self.glasses())
        chunks = []
        radio.fail_write = lambda kind, data: kind == 'ota' and data[4] == 0xe5 and chunks.append(1) is None \
            and len(chunks) == 50
        error, _ = attempt('install', radio)
        self.assertRegex(str(error), 'Stopped during block .*Injected disconnect.*keep running their current firmware')
        last = max(i for i, e in enumerate(radio.log) if e[0] == 'write')
        self.assertEqual(radio.log[last][1:3], ('ota', radio.writes('ota')[-1]))
        self.assertEqual(radio.ota_commands().count(0xe5), 50)
        self.assertNotIn(0xe6, radio.ota_commands())
        self.assertFalse(any(e[0] == 'connect' for e in radio.log[last:]))

    def test_control_link_loss_stops_the_transfer(self):
        radio = FakeRadio(self.glasses())
        radio.fail_drain = 200
        error, _ = attempt('install', radio)
        self.assertRegex(str(error), 'Injected control link loss')
        writes_after = radio.ota_commands()
        self.assertNotIn(0x03, writes_after)
        self.assertEqual(radio.drains, 200)

    def test_pacing_ack_waits_two_seconds_and_continues(self):
        glasses = self.glasses(pacing_after=3)
        radio = FakeRadio(glasses)
        result, _ = run_in('install', radio)
        self.assertEqual(result['paced_acks'], 1)
        self.assertEqual(sum(1 for e in radio.log if e == ('sleep', 0.1)), 20)
        self.assertEqual(glasses.revision, 25)

    def test_unknown_pacing_or_sequence_stops(self):
        for kwargs, reason in (({'pacing_after': 2, 'pacing_value': 1000}, 'pacing field 1000'),
                               ({'bad_ack_at': 4}, 'status/sequence')):
            with self.subTest(reason=reason):
                radio = FakeRadio(self.glasses(**kwargs))
                error, _ = attempt('install', radio)
                self.assertRegex(str(error), reason)
                self.assertNotIn(0xe6, radio.ota_commands())

    def test_unreachable_glasses_are_retried_while_nothing_is_written(self):
        glasses = self.glasses(boot_unreachable=3)
        radio = FakeRadio(glasses)
        radio.refuse_connects = 2
        result, _ = run_in('install', radio)
        self.assertEqual(result['booted_version'], 'GLASSLYCFW/25')

    def test_glasses_that_never_come_back_are_reported(self):
        radio = FakeRadio(self.glasses(boot_unreachable=10 ** 6))
        error, _ = attempt('install', radio)
        self.assertRegex(str(error), 'acknowledged the restart, but they did not answer within 5 minutes')

    def test_paired_glasses_are_required_once(self):
        radio = FakeRadio(self.glasses(), paired=[PairedDevice('11:22:33:44:55:66', 'Keyboard')])
        with mock.patch.object(memomind, 'WAIT_SECONDS', 10):
            error, files = attempt('install', radio)
        self.assertRegex(str(error), r'No MemoMind glasses are paired .*Keyboard \(11:22:33:44:55:66\)')
        self.assertNotIn(('connect', 'normal'), radio.log)
        radio = FakeRadio(self.glasses(), paired=[PairedDevice(ADDRESS, 'MemoMind One'),
                                                   PairedDevice('AA:BB:CC:DD:EE:02', 'memomind one')])
        error, _ = attempt('install', radio)
        self.assertRegex(str(error), 'More than one pair of MemoMind glasses.*AA:BB:CC:DD:EE:02')
        self.assertEqual(radio.log, [])

    def test_cancel_while_waiting_writes_nothing(self):
        radio = FakeRadio(self.glasses(), paired=[])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Cancelled):
                run('install', Path(folder), Recorder(cancel_after=3), link=radio)
        self.assertEqual([e for e in radio.log if e[0] != 'sleep'], [])


class BackupTests(unittest.TestCase):
    def test_backup_records_identity_and_writes_nothing(self):
        glasses = FakeGlasses(load_cfw(), revision=22)
        result, files = run_in('backup', FakeRadio(glasses))
        self.assertEqual(sorted(files), ['capabilities.json', 'device-info.json', 'trace.jsonl'])
        self.assertEqual(json.loads(files['device-info.json']), IDENTITY)
        self.assertEqual(json.loads(files['capabilities.json'])['version'], 'GLASSLYCFW/22')
        self.assertEqual((result['installed'], result['flash_modified'], result['firmware_image_saved']),
                         ('GLASSLYCFW/22', False, False))
        self.assertIn('no command that reads its flash back', result['note'])
        self.assertEqual(glasses.events, [('gm', 1, 1), ('gm', 1, 2), ('gm', 15, 0x7e)])

    def test_backup_of_an_unknown_kind_sends_only_the_probe(self):
        glasses = FakeGlasses(load_cfw(), identity={**IDENTITY, 'model': 'H009Z'})
        result, files = run_in('backup', FakeRadio(glasses))
        self.assertEqual(result['installed'], 'not queried')
        self.assertEqual(glasses.events, [('gm', 1, 1), ('gm', 1, 2)])
        self.assertNotIn('capabilities.json', files)


class TransportParsingTests(unittest.TestCase):
    def test_paired_device_listings(self):
        self.assertEqual(rfcomm.parse_mac_paired('4c-3c-8f-90-cc-c3\tMemoMind One\naa-bb-cc-dd-ee-ff\t\n'),
                         [PairedDevice('4C:3C:8F:90:CC:C3', 'MemoMind One'), PairedDevice('AA:BB:CC:DD:EE:FF', '')])
        text = ('\x1b[0;94m[bluetooth]\x1b[0m# Device 4C:3C:8F:90:CC:C3 MemoMind One\n'
                'Device AA:BB:CC:DD:EE:FF Keyboard K380\nWaiting to connect to bluetoothd...\n')
        self.assertEqual(rfcomm.parse_bluetoothctl(text),
                         [PairedDevice('4C:3C:8F:90:CC:C3', 'MemoMind One'),
                          PairedDevice('AA:BB:CC:DD:EE:FF', 'Keyboard K380')])
        self.assertEqual(rfcomm.select_glasses(rfcomm.parse_bluetoothctl(text)).address, '4C:3C:8F:90:CC:C3')
        self.assertIsNone(rfcomm.select_glasses([PairedDevice('AA:BB:CC:DD:EE:FF', 'Keyboard')]))

    def test_sdptool_channel(self):
        text = ('Searching for 0x7033 on 4C:3C:8F:90:CC:C3 ...\nService RecHandle: 0x10004\nService Class ID List:\n'
                '  UUID 128: 00007033-0000-1000-8000-00805f9b34fb\nProtocol Descriptor List:\n  "L2CAP" (0x0100)\n'
                '  "RFCOMM" (0x0003)\n    Channel: 5\n\n')
        self.assertEqual(rfcomm.parse_sdptool(text, 0x7033), 5)
        self.assertIsNone(rfcomm.parse_sdptool('Searching for 0x7034 on 4C:3C:8F:90:CC:C3 ...\n', 0x7034))

    def test_socket_addresses(self):
        self.assertEqual(rfcomm.sockaddr_bth('4C:3C:8F:90:CC:C3', 0x7034).hex(),
                         '2000c3cc908f3c4c0000' '34700000' '0000' '0010' '800000805f9b34fb' '00000000')
        self.assertEqual(rfcomm.sockaddr_rc('4c-3c-8f-90-cc-c3', 6).hex(), '1f00c3cc908f3c4c0600')
        self.assertEqual(rfcomm.format_address(0x4c3c8f90ccc3), '4C:3C:8F:90:CC:C3')
        self.assertEqual((rfcomm.ctypes.sizeof(rfcomm.BluetoothDeviceInfo),
                          rfcomm.ctypes.sizeof(rfcomm.BluetoothSearchParams)), (560, 40))


class ReplayChannel:
    """Plays one RFCOMM session of a hardware transcript. Writes must reproduce the recorded bytes in order; a read
    is only answered where the glasses had sent something."""

    def __init__(self, events, name, wire=None):
        self.events = events
        self.name = name
        self.position = 0
        self.partial = 0
        self.wire = wire

    def write(self, data):
        data = bytes(data)
        if self.wire is not None:
            self.wire.update(data)
        while data:
            if self.position >= len(self.events) or self.events[self.position][0] != 'tx':
                raise AssertionError(f'{self.name} event {self.position}: write the transcript does not have')
            expected = self.events[self.position][1][self.partial:]
            if data[:len(expected)] != expected[:len(data)]:
                raise AssertionError(f'{self.name} event {self.position}: bytes differ from the hardware transcript')
            taken = min(len(data), len(expected))
            data, self.partial = data[taken:], self.partial + taken
            if self.partial == len(self.events[self.position][1]):
                self.position, self.partial = self.position + 1, 0

    def read(self, timeout):
        data = self.drain()
        if not data:
            raise AssertionError(f'{self.name} event {self.position}: read where the transcript has no reply')
        return data

    def drain(self):
        out = b''
        while self.partial == 0 and self.position < len(self.events) and self.events[self.position][0] == 'rx':
            out += self.events[self.position][1]
            self.position += 1
            if self.name == 'ota':
                break
        return out

    def close(self):
        pass

    def finished(self):
        return self.position == len(self.events)


class ScriptRadio(FakeRadio):
    def __init__(self, glasses, address, script):
        super().__init__(glasses, paired=[PairedDevice(address, 'MemoMind One')])
        self.script = list(script)

    def connect(self, address, service):
        expected, make = self.script.pop(0)
        assert expected == service
        self.log.append(('connect', service))
        return make()


def native_trace(path):
    sessions = {'control': [], 'ota': []}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        kind = {'transmitSubmission': 'tx', 'receive': 'rx'}.get(row['event'])
        if kind and row.get('session') in sessions:
            sessions[row['session']].append((kind, bytes.fromhex(row['hex'])))
    return sessions


def device_info(events):
    parser = GmStream()
    for kind, data in events:
        for message in parser.feed(data) if kind == 'rx' else ():
            if (message.service, message.command) == (1, 2) and message.tlvs:
                return json.loads(message.tlvs[0][1].rstrip(b'\0'))
    raise AssertionError('No device information in the transcript')


class HardwareReplayTests(unittest.TestCase):
    @unittest.skipUnless(INSTALL_25.exists(), 'revision 25 install trace unavailable')
    def test_revision_25_install_byte_for_byte(self):
        self.assertEqual(hashlib.sha256(INSTALL_25.read_bytes()).hexdigest(), INSTALL_25_SHA)
        sessions = native_trace(INSTALL_25)
        identity = device_info(sessions['control'])
        glasses = FakeGlasses(load_cfw(), revision=22, identity=identity)
        control, ota = ReplayChannel(sessions['control'], 'control'), ReplayChannel(sessions['ota'], 'ota')

        def booted():
            glasses.revision = 25
            return FakeChannel(radio, 'normal')

        radio = ScriptRadio(glasses, identity['mac'], [('normal', lambda: FakeChannel(radio, 'normal')),
                                                       ('normal', lambda: control), ('ota', lambda: ota),
                                                       ('normal', booted)])
        with tempfile.TemporaryDirectory() as folder:
            result = run('install', Path(folder), Recorder(), link=radio)
        self.assertTrue(ota.finished())
        self.assertFalse([e for e in control.events[control.position:] if e[0] == 'tx'])
        self.assertEqual((result['blocks'], result['chunks'], result['transferred_range']), (548, 3834, [18, 2243544]))
        self.assertEqual((result['installed_before'], result['booted_version']), ('GLASSLYCFW/22', 'GLASSLYCFW/25'))

    @unittest.skipUnless(RESUME_18.exists() and IMAGE_18.exists(), 'revision 18 resume capture unavailable')
    def test_revision_18_resume_byte_for_byte(self):
        sessions = native_trace(RESUME_18)
        identity = device_info(sessions['control'])
        control, ota = ReplayChannel(sessions['control'], 'control'), ReplayChannel(sessions['ota'], 'ota')
        radio = ScriptRadio(None, identity['mac'], [('normal', lambda: control), ('ota', lambda: ota)])
        with tempfile.TemporaryDirectory() as folder, (Path(folder) / 'trace.jsonl').open('x') as trace:
            result = Session(radio, Path(folder), Recorder(), trace).flash(identity['mac'], IMAGE_18.read_bytes(),
                                                                          identity)
        self.assertTrue(ota.finished())
        self.assertEqual((result['resumed_from'], result['blocks'], result['chunks'], result['transferred_range']),
                         (1186912, 260, 1819, [1186912, 2251153]))

    @unittest.skipUnless(VENDOR_TSV.exists() and STOCK.exists(), 'vendor update capture unavailable')
    def test_vendor_app_update_byte_for_byte(self):
        control_events, ota_events, before = [], [], []
        for line in VENDOR_TSV.read_text().splitlines():
            number, _, direction, _, channel, raw = line.split('\t')
            event = ('tx' if int(direction, 0) == 0 else 'rx', bytes.fromhex(raw))
            if int(number) in (1615, 1617):
                control_events.append(event)
            elif int(channel) == 6:
                ota_events.append(event)
            elif int(channel) == 5 and int(number) < 1615:
                before.append(event)
        identity = device_info(before)
        self.assertEqual(identity['ver'], '1.0.2.151')
        wire = hashlib.sha256()
        control = ReplayChannel(control_events, 'control', wire)
        ota = ReplayChannel(ota_events, 'ota', wire)
        GmSession(control).notify_ota()
        image = STOCK.read_bytes()
        result = OtaTransaction(ota, image, identity, identity['mac'], check_control=control.drain,
                                sleep=lambda seconds: None, pace=0).run()
        self.assertTrue(control.finished() and ota.finished())
        self.assertEqual((result['blocks'], result['chunks'], result['transferred_range'], result['resumed_from']),
                         (494, 3457, [162757, len(image)], 162757))
        self.assertEqual(wire.hexdigest(), 'f4acaa5ca1a8a02001328eebc88c52be4ab65216cf8f42c70e09e77dda294004')


if __name__ == '__main__':
    unittest.main()
