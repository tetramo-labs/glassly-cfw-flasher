"""Jieli link layer with fake devices: pass-through discovery and residues, Bulk-Only framing, waiting."""
import contextlib
import ctypes
import io
import struct
import unittest

from glassly_flasher.devices.ananke.link import BR23_INQUIRY, Br23PassThroughLink
from glassly_flasher.devices.base import Cancelled, FlashError, Reporter
from glassly_flasher.devices.gamebrick import is_bd19_uboot
from glassly_flasher.devices.gamebrick.transport import Transport
from glassly_flasher.devices.jieli import Watchdog, wait_for_device
from glassly_flasher.devices.jieli.link import (INQUIRY, AmbiguousTarget, BulkOnlyLink, LinkError,
                                                PassThroughLink, candidate_paths)
from glassly_flasher.devices.jieli.vendored import jl_crc16

BD19 = bytes(8) + b'BD19    ' + b'UBOOT1.00       ' + b'1.00'
SSD = bytes(8) + b'ATA     ' + b'Some SSD        ' + b'1.0 '


class FakeSCSIDev:
    """Mimics jl-uboot-tool's SCSIDev: execute(cdb, data_out, data_in) -> residue."""
    devices = {}

    def __init__(self):
        self.path = None

    def open(self, path):
        if path not in self.devices:
            raise OSError('no such device')
        self.path = path
        self.devices[path]['open'] = True

    def close(self):
        self.devices[self.path]['open'] = False

    def execute(self, cdb, data_out, data_in):
        dev = self.devices[self.path]
        dev.setdefault('log', []).append((bytes(cdb), data_out, None if data_in is None else len(data_in)))
        if cdb[0] == 0x12:
            data_in[:] = dev['inquiry'][:len(data_in)]
            return 0
        if data_in is not None:
            reply = dev.get('reply', b'\xa5' * len(data_in))
            data_in[:len(reply)] = reply
        return dev.get('residue', 0)


class PassThroughTests(unittest.TestCase):
    def setUp(self):
        FakeSCSIDev.devices = {'/dev/sg0': {'inquiry': SSD}, '/dev/sg1': {'inquiry': BD19}}

    def test_discovery_sends_only_an_inquiry_to_other_disks(self):
        link = PassThroughLink(is_bd19_uboot, FakeSCSIDev, ['/dev/sg9', '/dev/sg0', '/dev/sg1'])
        self.assertTrue(link.try_open())
        self.assertEqual(link.path, '/dev/sg1')
        self.assertEqual(FakeSCSIDev.devices['/dev/sg0']['log'], [(INQUIRY, None, 36)])
        self.assertFalse(FakeSCSIDev.devices['/dev/sg0']['open'])
        FakeSCSIDev.devices['/dev/sg1']['inquiry'] = bytes(8) + b'BD19    ' + b'UDISK           ' + b'1.00'
        self.assertFalse(PassThroughLink(is_bd19_uboot, FakeSCSIDev, ['/dev/sg1']).try_open())
        self.assertFalse(PassThroughLink(is_bd19_uboot, FakeSCSIDev, []).try_open())

    def test_two_controllers_are_refused_and_released(self):
        FakeSCSIDev.devices['/dev/sg2'] = {'inquiry': BD19}
        link = PassThroughLink(is_bd19_uboot, FakeSCSIDev, ['/dev/sg0', '/dev/sg1', '/dev/sg2'])
        with self.assertRaises(AmbiguousTarget):
            link.try_open()
        self.assertFalse(FakeSCSIDev.devices['/dev/sg1']['open'] or FakeSCSIDev.devices['/dev/sg2']['open'])
        self.assertIsNone(link.dev)

    def test_commands_route_data_and_reject_unexpected_residue(self):
        link = PassThroughLink(is_bd19_uboot, FakeSCSIDev, ['/dev/sg1'])
        link.try_open()
        dev = FakeSCSIDev.devices['/dev/sg1']
        dev['reply'] = b'\xfc\x0a' + bytes(14)
        self.assertEqual(link.execute(b'\xfc\x0a'.ljust(16, b'\xff'), None, 16), dev['reply'])
        self.assertEqual(link.execute(b'\xfb\x06' + bytes(14), b'\x11' * 512, 0), b'')
        self.assertEqual(dev['log'][-1], (b'\xfb\x06' + bytes(14), b'\x11' * 512, None))
        dev['residue'] = 16
        with self.assertRaisesRegex(LinkError, 'residue'):
            link.execute(b'\xfd\x05'.ljust(16, b'\xff'), None, 256)
        self.assertFalse(link.last_command['complete'])
        link.close()
        with self.assertRaises(LinkError):
            link.execute(INQUIRY, None, 36)

    def test_br23_residue_is_exact_on_linux_and_may_be_hidden_on_windows(self):
        FakeSCSIDev.devices = {'/dev/sg1': {'inquiry': BR23_INQUIRY, 'reply': b'\xfc\x0a' + bytes(14)}}
        query = b'\xfc\x0a' + b'\xff' * 14
        for platform, residue, ok in (('linux', 16, True), ('linux', 0, False), ('linux', 5, False),
                                      ('win32', 16, True), ('win32', 0, True), ('win32', 5, False)):
            with self.subTest(platform=platform, residue=residue):
                link = Br23PassThroughLink(FakeSCSIDev, ['/dev/sg1'], platform)
                self.assertTrue(link.try_open())
                link.loader_queries_enabled = True
                FakeSCSIDev.devices['/dev/sg1']['residue'] = residue
                if ok:
                    self.assertEqual(link.execute(query, n=16)[:2], b'\xfc\x0a')
                    self.assertIn(0xfc0a, link.loader_query_replies)
                else:
                    with self.assertRaises(LinkError):
                        link.execute(query, n=16)

    def test_br23_identity_must_match_all_36_bytes(self):
        FakeSCSIDev.devices = {'/dev/sg1': {'inquiry': BR23_INQUIRY[:-1] + b'1'}}
        self.assertFalse(Br23PassThroughLink(FakeSCSIDev, ['/dev/sg1'], 'linux').try_open())

    def test_transport_allowlist_runs_before_the_link(self):
        link = PassThroughLink(is_bd19_uboot, FakeSCSIDev, ['/dev/sg1'])
        link.try_open()
        t = Transport(link, [0x3000])
        with self.assertRaises(RuntimeError):
            t.command(0xfb01, (0x3000).to_bytes(4, 'big'))
        self.assertEqual(len(FakeSCSIDev.devices['/dev/sg1']['log']), 1, 'only the discovery INQUIRY reached the device')
        self.assertEqual(t.execute(INQUIRY, n=36), BD19)

    def test_candidate_paths(self):
        self.assertEqual(candidate_paths('win32')[0], '\\\\.\\PhysicalDrive0')
        self.assertEqual(len(candidate_paths('win32')), 32)
        self.assertEqual(candidate_paths('darwin'), [])


class FakeNative:
    """The IOUSBHost helper: endpoint 1 out, 0x81 in. Records CBWs and answers with data and a CSW."""

    def __init__(self, opens=(-2, 0)):
        self.out = []
        self.queue = []
        self.opens = list(opens)
        self.error = b'none'

    def jl_open(self, vendor, product):
        self.opened_with = (vendor, product)
        return self.opens.pop(0)

    def jl_close(self):
        pass

    def jl_error(self):
        return self.error

    def jl_transfer(self, ep, buf, length, timeout):
        if ep == 1:
            data = ctypes.string_at(buf, length)
            self.out.append(data)
            if data[:4] == b'USBC':
                tag, size, flags = struct.unpack_from('<IIB', data, 4)
                if flags & 0x80 and size:
                    self.queue.append(bytes(i % 256 for i in range(size)))
                self.queue.append(struct.pack('<4sIIB', b'USBS', tag, 0, 0))
            return length
        reply = self.queue.pop(0)[:length]
        ctypes.memmove(buf, reply, len(reply))
        return len(reply)


class BulkOnlyTests(unittest.TestCase):
    def test_bulk_only_framing_and_status(self):
        native = FakeNative()
        link = BulkOnlyLink(native, 0x4c4a, 0x3442)
        self.assertFalse(link.try_open())
        self.assertTrue(link.try_open())
        self.assertEqual(native.opened_with, (0x4c4a, 0x3442))
        data = link.execute(b'\xfd\x05' + struct.pack('>IH', 0x1000, 256) + bytes(8), None, 256)
        self.assertEqual(len(data), 256)
        cbw = native.out[0]
        self.assertEqual(cbw[:4], b'USBC')
        self.assertEqual(struct.unpack_from('<IIBBB', cbw, 4), (1, 256, 0x80, 0, 16))
        link.execute(b'\xfb\x06' + bytes(14), b'\x22' * 512, 0)
        self.assertEqual(native.out[-1], b'\x22' * 512)
        native.queue.append(struct.pack('<4sIIB', b'USBS', 99, 0, 1))
        with self.assertRaisesRegex(LinkError, 'status'):
            link.execute(b'\xfc\x0a'.ljust(16, b'\xff'), None, 0)
        self.assertFalse(link.last_command['complete'])

    def test_open_results(self):
        native = FakeNative(opens=(-3, -1, -1))
        link = BulkOnlyLink(native, 0x4c4a, 0x2342)
        with self.assertRaises(AmbiguousTarget):
            link.try_open()
        native.error = b'Capture: Error Domain=NSOSStatusErrorDomain Code=-536870207 (0xe00002c1)'
        with self.assertRaisesRegex(LinkError, 'administrator'):
            link.try_open()
        native.error = b'Configure: failed'
        with self.assertRaisesRegex(LinkError, 'Configure'):
            link.try_open()


class Recorder(Reporter):
    def __init__(self, cancel_after=None):
        self.statuses, self.logs, self.polls, self.cancel_after = [], [], 0, cancel_after

    def log(self, text):
        self.logs.append(text)

    def status(self, text):
        self.statuses.append(text)

    def progress(self, done, total):
        pass

    def cancelled(self):
        self.polls += 1
        return self.cancel_after is not None and self.polls > self.cancel_after


class WaitTests(unittest.TestCase):
    class Never:
        def try_open(self):
            return False

    def test_cancel_while_waiting(self):
        reporter = Recorder(cancel_after=3)
        with self.assertRaises(Cancelled):
            wait_for_device(self.Never(), reporter, sleep=lambda _: None)
        self.assertEqual(reporter.statuses, ['Waiting for the controller in update mode…'])

    def test_timeout(self):
        times = iter([0, 1, 700])
        with self.assertRaisesRegex(FlashError, '10 minutes'):
            wait_for_device(self.Never(), Recorder(), clock=lambda: next(times), sleep=lambda _: None)

    def test_ambiguous_target_propagates(self):
        class Two:
            def try_open(self):
                raise AmbiguousTarget('two')
        with self.assertRaises(AmbiguousTarget):
            wait_for_device(Two(), Recorder(), sleep=lambda _: None)

    def test_watchdog_exits_on_expiry_only(self):
        codes = []
        dog = Watchdog(60, exit=codes.append)
        with dog:
            pass
        dog.timer.join(5)
        self.assertFalse(dog.timer.is_alive())
        self.assertEqual(codes, [])
        with contextlib.redirect_stdout(io.StringIO()):
            dog.expire()
        self.assertEqual(codes, [124])


class VendoredTests(unittest.TestCase):
    def test_crc16_matches_the_xmodem_check_value(self):
        self.assertEqual(jl_crc16(b'123456789'), 0x31c3)
        self.assertEqual(jl_crc16(b'123456789', 0x1234), jl_crc16(b'56789', jl_crc16(b'1234', 0x1234)))


if __name__ == '__main__':
    unittest.main()
