from pathlib import Path
import struct
import tempfile
import unittest

from glassly_flasher.devices.base import FlashError, Reporter
from glassly_flasher.devices.eightbitdo import (BASE, META_BASE, META_SIZE, crc16, is_read_reply,
                                                load_image, packet, session)
from glassly_flasher.devices.eightbitdo import micro, zero2

ZERO2_HARDWARE = Path('/Users/fosse/dev/glassly-8bitdo-zero2-cfw/build/hardware')
MICRO_HARDWARE = Path('/Users/fosse/dev/glassly-8bitdo-micro/build/hardware')


class QuietReporter(Reporter):
    def __init__(self):
        self.lines = []

    def log(self, text):
        self.lines.append(text)

    def status(self, text):
        self.lines.append(text)

    def progress(self, done, total):
        pass


def reply(cmd, data=b'', total=0, address=0):
    out = bytearray(64)
    out[1] = 5
    struct.pack_into('<HHH', out, 4, cmd, len(data), crc16(data))
    struct.pack_into('<II', out, 10, total, address)
    out[18:18 + len(data)] = data
    return bytes(out)


class FakeBootloader:
    def __init__(self, profile, application, version=None, corrupt=False):
        self.profile = profile
        self.application = bytearray(application)
        self.metadata = bytearray(META_SIZE)
        struct.pack_into('<I', self.metadata, 0x40, profile.version if version is None else version)
        self.cipher = bytearray(profile.size)
        self.known = {}
        for image in profile.images.values():
            dat, plain = load_image(profile, image)
            start = profile.header_offset + 28
            self.known[dat[start:start + profile.size]] = plain
        self.corrupt = corrupt
        self.commands = []
        self.pending = None

    def drain(self):
        self.pending = None

    def close(self):
        pass

    def receive(self, timeout):
        out, self.pending = self.pending, None
        return out

    def memory(self, address, n):
        if BASE <= address < BASE + self.profile.size:
            return bytes(self.application[address - BASE:address - BASE + n])
        return bytes(self.metadata[address - META_BASE:address - META_BASE + n])

    def send(self, data):
        cmd, n, crc, total, address = struct.unpack_from('<HxxHHII', data, 1)
        payload = data[17:17 + n]
        assert data[0] == 5 and crc == crc16(data[17:17 + n])
        self.commands.append((cmd, address))
        if cmd == 8:
            self.pending = reply(8, struct.pack('<HH', self.profile.boot_product, 0), 4)
        elif cmd == 5:
            self.pending = reply(5, self.memory(address, n), n, address)
        elif cmd == 4:
            if address == BASE:
                self.application[:] = b'\xff' * self.profile.size
            else:
                self.metadata[0x40:0x60] = b'\xff' * 32
            self.pending = reply(4)
        elif cmd == 3:
            offset = address - BASE
            self.cipher[offset:offset + n] = payload
            if offset + n == self.profile.size:
                plain = bytearray(self.known.get(bytes(self.cipher), bytes(self.profile.size)))
                if self.corrupt:
                    plain[100] ^= 1
                self.application[:] = plain
            self.pending = reply(3)
        elif cmd == 2:
            self.metadata[0x40:0x44] = payload
            self.pending = reply(2)
        elif cmd == 7:
            self.pending = None


class ReplayLink:
    def __init__(self, transcript):
        self.lines = [line.strip() for line in transcript.read_text().splitlines() if line[:3] in ('tx=', 'rx=')]
        self.position = 0

    def drain(self):
        pass

    def close(self):
        pass

    def send(self, data):
        expected = self.lines[self.position]
        if expected != 'tx=' + data.hex():
            raise AssertionError(f'Packet {self.position} differs from the hardware transcript')
        self.position += 1

    def receive(self, timeout):
        if self.position < len(self.lines) and self.lines[self.position].startswith('rx='):
            self.position += 1
            return bytes.fromhex(self.lines[self.position - 1][3:])
        return None


def image(profile, mode):
    return load_image(profile, profile.images[mode])


class PacketTests(unittest.TestCase):
    def test_packet_matches_c_framing(self):
        self.assertEqual(packet(8, 0, 2, 2).hex(), '0508000000020001b002000000000000' + '00' * 48)

    def test_read_reply_checks_crc_length_and_address(self):
        good = reply(5, bytes(range(32)), 32, BASE)
        self.assertTrue(is_read_reply(good, 5, BASE, 32))
        self.assertFalse(is_read_reply(good, 5, BASE + 32, 32))
        bad = bytearray(good)
        bad[20] ^= 1
        self.assertFalse(is_read_reply(bytes(bad), 5, BASE, 32))


class FakeDeviceTests(unittest.TestCase):
    def run_session(self, profile, mode, device):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            result = session(profile, mode, device, output, QuietReporter(), image(profile, mode))
            return result, sorted(p.name for p in output.iterdir())

    def test_install_and_restore_each_profile(self):
        for profile in (zero2.PROFILE, micro.PROFILE):
            official = image(profile, 'restore')[1]
            glassly = image(profile, 'install')[1]
            device = FakeBootloader(profile, official)
            result, files = self.run_session(profile, 'install', device)
            self.assertEqual(bytes(device.application), glassly)
            self.assertEqual(result['baseline'], f'official {profile.official}')
            self.assertEqual(struct.unpack_from('<I', device.metadata, 0x40)[0], profile.version)
            self.assertEqual(device.commands[-1], (7, 0))
            self.assertIn('before-application.bin', files)
            self.assertIn('after-metadata.bin', files)
            self.run_session(profile, 'restore', device)
            self.assertEqual(bytes(device.application), official)

    def test_unknown_baseline_is_never_erased(self):
        device = FakeBootloader(zero2.PROFILE, b'\x42' * zero2.PROFILE.size)
        with self.assertRaisesRegex(FlashError, 'does not recognise'):
            self.run_session(zero2.PROFILE, 'install', device)
        self.assertFalse({cmd for cmd, _ in device.commands} & {2, 3, 4, 7})

    def test_wrong_version_record_is_never_erased(self):
        device = FakeBootloader(micro.PROFILE, image(micro.PROFILE, 'restore')[1], version=104)
        with self.assertRaisesRegex(FlashError, 'version record'):
            self.run_session(micro.PROFILE, 'install', device)
        self.assertFalse({cmd for cmd, _ in device.commands} & {2, 3, 4, 7})

    def test_wrong_controller_is_refused_before_reading(self):
        device = FakeBootloader(micro.PROFILE, image(micro.PROFILE, 'restore')[1])
        with self.assertRaisesRegex(FlashError, 'not an 8BitDo Zero 2'):
            self.run_session(zero2.PROFILE, 'install', device)
        self.assertEqual(device.commands, [(8, 0)])

    def test_failed_verification_never_commits(self):
        device = FakeBootloader(zero2.PROFILE, image(zero2.PROFILE, 'restore')[1], corrupt=True)
        with self.assertRaisesRegex(FlashError, 'did not read back'):
            self.run_session(zero2.PROFILE, 'install', device)
        self.assertFalse({cmd for cmd, _ in device.commands} & {2, 7})


class HardwareReplayTests(unittest.TestCase):
    """Replays real flashes recorded by the C flashers; skipped without the private captures."""

    def replay(self, profile, folder, expect_error=None):
        if not folder.exists():
            self.skipTest('private hardware capture not present')
        dat, plain = (folder / 'input.dat').read_bytes(), (folder / 'input-plaintext.bin').read_bytes()
        link = ReplayLink(folder / 'transport.log')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            if expect_error:
                with self.assertRaisesRegex(FlashError, expect_error):
                    session(profile, 'install', link, output, QuietReporter(), (dat, plain))
            else:
                session(profile, 'install', link, output, QuietReporter(), (dat, plain))
                self.assertEqual((output / 'after-application.bin').read_bytes(),
                                 (folder / 'after-application.bin').read_bytes())
            self.assertEqual((output / 'before-application.bin').read_bytes(),
                             (folder / 'before-application.bin').read_bytes())
        self.assertEqual(link.position, len(link.lines))

    def test_zero2_buttons_flash(self):
        self.replay(zero2.PROFILE, ZERO2_HARDWARE / '20261105T212350.914597Z-script-flash')

    def test_micro_buttons_flash(self):
        self.replay(micro.PROFILE, MICRO_HARDWARE / '20261106T085451.634428Z-script-flash')

    def test_micro_stock_flash_from_factory(self):
        self.replay(micro.PROFILE, MICRO_HARDWARE / '20261106T072004.211887Z-script-flash')

    def test_micro_unknown_factory_tail_refused(self):
        self.replay(micro.PROFILE, MICRO_HARDWARE / '20261106T084134.929205Z-script-flash', 'does not recognise')


if __name__ == '__main__':
    unittest.main()
