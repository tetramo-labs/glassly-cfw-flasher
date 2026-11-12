"""Gamebrick Mini planning, programming and run() against fake devices. No USB access.

Tests marked with FIXTURES use the unit backups in a local glassly-gamebrick-mini checkout and
skip when it is absent."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from glassly_flasher.devices.base import FlashError, Reporter
from glassly_flasher.devices.gamebrick import run
from glassly_flasher.devices.gamebrick.image import (CUSTOM_APP_SHA, OEM_SHA, classify, digest, glassly_application,
                                                      official_application, plan)
from glassly_flasher.devices.gamebrick.layout import APPLICATIONS, LAYOUTS, Layout
from glassly_flasher.devices.gamebrick.transport import PAGE, SECTOR, SIZE, program_image, program_with_plan
from glassly_flasher.devices.jieli.link import INQUIRY

RESEARCH = Path('/Users/fosse/dev/glassly-gamebrick-mini/research')
FACTORY = RESEARCH/'device-backup-20261028-192851/flash-pass1.bin'
UNIT1 = RESEARCH/'device-backup-20261022-233350/flash-pass1.bin'
CUSTOM = RESEARCH/'custom-prepared/unit2/custom-device-layout.bin'
FLASHED = RESEARCH/'custom-flash-unit2-20261028-193401/target.bin'
WITHDRAWN = RESEARCH/'custom-prepared/custom-device-layout-withdrawn-20261025.bin'
TESTER = RESEARCH/'device-backup-20261113-tester/flash-pass1.bin'
UNIT2_STOCK_SHA = '51b28f4364802db4597baa3a492a2d2b6ce244f3574a6b779542c07ee242f488'
FIXTURES = unittest.skipUnless(all(p.exists() for p in (FACTORY, UNIT1, CUSTOM, FLASHED, WITHDRAWN)),
                               'Gamebrick unit backups unavailable')
BD19 = bytes(8) + b'BD19    ' + b'UBOOT1.00       ' + b'1.00'


class NOR:
    def __init__(self, data, sectors, fail=None):
        self.data = bytearray(data)
        self.sectors = set(sectors)
        self.writes_enabled = False
        self.erased = []
        self.pages = 0
        self.fail = fail

    def read(self, a, n):
        return bytes(self.data[a:a+n])

    def erase(self, a):
        assert self.writes_enabled and a in self.sectors and a % SECTOR == 0
        self.erased.append(a)
        self.data[a:a+SECTOR] = b'\xff'*SECTOR

    def program(self, a, b):
        assert self.writes_enabled and a//SECTOR*SECTOR in self.sectors and len(b) == PAGE and a % PAGE == 0
        self.pages += 1
        if self.pages == self.fail:
            raise RuntimeError('Injected disconnect')
        assert all(x & y == y for x, y in zip(self.data[a:a+PAGE], b))
        self.data[a:a+PAGE] = b


class FakeBD19:
    """BD19 ROM + pinned loader + 512 KiB NOR behind the link interface."""

    transport = 'fake'

    def __init__(self, flash, absent_polls=0, flash_id=0xcd7113):
        self.flash = bytearray(flash)
        self.flash_id = flash_id
        self.ram = bytearray(23040)
        self.calls = []
        self.erased = []
        self.polls = 0
        self.absent_polls = absent_polls
        self.closed = False
        self.last_command = None

    def try_open(self):
        self.polls += 1
        return self.polls > self.absent_polls

    def close(self):
        self.closed = True

    def execute(self, cdb, out=None, n=0):
        self.calls.append(bytes(cdb[:2]))
        op = int.from_bytes(cdb[:2], 'big')
        addr = int.from_bytes(cdb[2:6], 'big')
        if cdb == INQUIRY:
            return BD19
        if op == 0xfb06:
            self.ram[addr-0x2000:addr-0x2000+len(out)] = out
            return b''
        if op == 0xfd07:
            return bytes(self.ram[addr-0x2000:addr-0x2000+n])
        if op == 0xfb04:
            for i, b in enumerate(out):
                self.flash[addr+i] &= b
            return b''
        if op == 0xfd05:
            return bytes(self.flash[addr:addr+n])
        if op == 0xfb01:
            self.erased.append(addr)
            self.flash[addr:addr+SECTOR] = b'\xff'*SECTOR
        replies = {0xfc0a: bytes([3, 0]) + self.flash_id.to_bytes(4, 'little'),
                   0xfc0b: (self.flash_id >> 8).to_bytes(3, 'big'),
                   0xfc14: bytes.fromhex('00000100')}
        return (cdb[:2] + replies.get(op, b'')).ljust(n, b'\0')

    def writes(self):
        return [c for c in self.calls if c in (b'\xfb\x01', b'\xfb\x04')]


class Recorder(Reporter):
    def __init__(self):
        self.statuses = []

    def log(self, text):
        pass

    def status(self, text):
        self.statuses.append(text)

    def progress(self, done, total):
        assert 0 <= done <= total


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class BundledApplicationTests(unittest.TestCase):
    def test_bundled_applications_are_pinned(self):
        self.assertEqual(digest(official_application()), OEM_SHA)
        self.assertEqual(digest(glassly_application()), CUSTOM_APP_SHA)
        self.assertEqual((len(LAYOUTS), len(APPLICATIONS)), (2, 5))

    def test_backup_of_unknown_flash_writes_nothing(self):
        image = bytes(range(256)) * (SIZE // 256)
        device = FakeBD19(image, absent_polls=2)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            report = quiet(run, 'backup', folder, Recorder(), link=device)
            self.assertEqual(report['state'], 'unknown')
            self.assertEqual((folder/'flash-pass1.bin').read_bytes(), image)
            self.assertEqual((folder/'flash-pass2.bin').read_bytes(), image)
        self.assertEqual(device.writes(), [])
        self.assertTrue(device.closed)

    def test_install_on_unknown_flash_is_refused_before_any_write(self):
        device = FakeBD19(bytes(range(256)) * (SIZE // 256))
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FlashError, 'Unrecognised'):
                quiet(run, 'install', Path(folder), Recorder(), link=device)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['flash-pass1.bin', 'flash-pass2.bin'])
        self.assertEqual(device.writes(), [])

    def test_second_known_flash_part_is_accepted(self):
        image = bytes(range(256)) * (SIZE // 256)
        device = FakeBD19(image, flash_id=0xcd7013)
        with tempfile.TemporaryDirectory() as folder:
            report = quiet(run, 'backup', Path(folder), Recorder(), link=device)
            self.assertEqual(report['state'], 'unknown')
            self.assertEqual((Path(folder)/'flash-pass2.bin').read_bytes(), image)

    def test_other_flash_chip_is_refused_with_its_identity(self):
        device = FakeBD19(bytes(SIZE), flash_id=0x0b4013)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FlashError, 'flash ID 0x0b4013.*read-ID 000b40.*Nothing was written'):
                quiet(run, 'install', Path(folder), Recorder(), link=device)
        self.assertEqual(device.writes(), [])
        self.assertNotIn(b'\xfd\x05', device.calls)

    def test_wrong_bootloader_is_refused(self):
        device = FakeBD19(bytes(SIZE))
        device.execute = lambda cdb, out=None, n=0: bytes(8) + b'BR23    ' + b'UBOOT1.00       ' + b'1.00'
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FlashError, 'Wrong controller bootloader'):
                quiet(run, 'backup', Path(folder), Recorder(), link=device)


@FIXTURES
class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.factory = FACTORY.read_bytes()
        cls.custom = CUSTOM.read_bytes()
        cls.oem = official_application()
        cls.stock = plan(cls.factory, cls.oem, 'restore')['target']

    def test_layouts_and_applications_classify_every_known_state(self):
        self.assertEqual(digest(self.stock), UNIT2_STOCK_SHA)
        self.assertEqual(classify(self.factory), 'factory')
        self.assertEqual(classify(self.stock), 'stock')
        self.assertEqual(classify(self.custom), 'custom')
        self.assertEqual(classify(FLASHED.read_bytes()), 'custom')
        self.assertEqual(classify(UNIT1.read_bytes()), 'factory', 'unit1 shares the layout')
        drifted = bytearray(self.factory)
        drifted[0x7a123] ^= 0xFF
        drifted[0x7f000] = 0x12
        self.assertEqual(classify(bytes(drifted)), 'factory', 'live storage does not change the classification')
        for address in (0, 0x2140, 0x79fff):
            bad = bytearray(self.factory)
            bad[address] ^= 1
            self.assertEqual(classify(bytes(bad)), 'unknown')

    def test_plans_from_every_state_reach_the_reviewed_images(self):
        from_factory = plan(self.factory, self.oem, 'install')
        self.assertEqual(from_factory['state'], 'factory')
        self.assertEqual(from_factory['target'], self.custom)
        self.assertEqual(len(from_factory['changed_sectors']), 50)
        from_stock = plan(self.stock, self.oem, 'install')
        self.assertEqual(from_stock['target'], self.custom)
        self.assertEqual([hex(a) for a in from_stock['changed_sectors']],
                         ['0x2000', '0x6000', '0x7000', '0xd000', '0x11000', '0x12000', '0x32000', '0x33000'])
        self.assertEqual(plan(self.custom, self.oem, 'install')['changed_sectors'], [])
        self.assertEqual(plan(self.custom, self.oem, 'restore')['target'], self.stock)
        drifted = bytearray(self.factory)
        drifted[0x7a123] ^= 0xFF
        live = plan(bytes(drifted), self.oem, 'install')
        self.assertEqual(live['target'][0x7a000:], bytes(drifted)[0x7a000:])
        self.assertEqual(live['target'][:0x7a000], self.custom[:0x7a000])
        for image in (WITHDRAWN.read_bytes(), bytes(b ^ (i == 0x2140) for i, b in enumerate(self.factory))):
            with self.assertRaisesRegex(RuntimeError, 'Unrecognised'):
                plan(image, self.oem, 'install')

    def test_programs_factory_to_custom_and_back_with_full_verification(self):
        oem = self.oem
        device = NOR(self.factory, range(0, SIZE, SECTOR))
        saved = {}
        report = quiet(program_with_plan, device, lambda c: (c, plan(c, oem, 'install')['target']), saved.__setitem__)
        self.assertTrue(report['full_readback_verified'])
        self.assertEqual(bytes(device.data), self.custom)
        self.assertEqual(saved['flash-pass1.bin'], self.factory)
        self.assertEqual(device.erased[-1], 0x2000, 'the header sector is committed last')
        self.assertFalse(device.writes_enabled)
        again = quiet(program_with_plan, device, lambda c: (c, plan(c, oem, 'install')['target']), saved.__setitem__)
        self.assertEqual(again['writes'], 0)
        quiet(program_with_plan, device, lambda c: (c, plan(c, oem, 'restore')['target']), saved.__setitem__)
        self.assertEqual(bytes(device.data), self.stock)

    def test_every_page_failure_leaves_protected_sectors_and_restores(self):
        target = plan(self.stock, self.oem, 'install')['target']
        sectors = plan(self.stock, self.oem, 'install')['changed_sectors']
        baseline = NOR(self.stock, sectors)
        baseline.writes_enabled = True
        quiet(program_image, baseline, self.stock, target, 'install')
        for page in range(1, baseline.pages + 1, 7):
            with self.subTest(page=page):
                d = NOR(self.stock, sectors, page)
                d.writes_enabled = True
                with self.assertRaises(RuntimeError):
                    quiet(program_image, d, self.stock, target, 'install')
                for a in range(0, SIZE, SECTOR):
                    if a not in d.sectors:
                        self.assertEqual(bytes(d.data[a:a+SECTOR]), self.stock[a:a+SECTOR])
                d.fail = None
                quiet(program_image, d, self.stock, target, 'restore')
                self.assertEqual(bytes(d.data), self.stock)

    def test_unstable_flash_never_enables_writes(self):
        oem = self.oem

        class Unstable(NOR):
            reads = 0

            def read(self, a, n):
                self.reads += 1
                if self.reads == 9:
                    self.data[0x7a000] ^= 1
                return super().read(a, n)
        shaky = Unstable(self.factory, range(0, SIZE, SECTOR))
        with self.assertRaisesRegex(RuntimeError, 'changed between preflight reads'):
            quiet(program_with_plan, shaky, lambda c: (c, plan(c, oem, 'install')['target']), lambda n, d: None)
        self.assertFalse(shaky.erased)

    def test_run_installs_restores_and_is_idempotent(self):
        device = FakeBD19(self.factory, absent_polls=1)
        with tempfile.TemporaryDirectory() as folder:
            report = quiet(run, 'install', Path(folder), Recorder(), link=device)
            names = sorted(p.name for p in Path(folder).iterdir())
        self.assertEqual(names, ['flash-pass1.bin', 'flash-pass2.bin', 'stock.bin', 'target.bin'])
        self.assertEqual(bytes(device.flash), self.custom)
        self.assertTrue(report['full_readback_verified'])
        self.assertEqual(report['state'], 'factory')
        self.assertEqual(device.erased[-1], 0x2000)
        self.assertLessEqual(max(device.erased) + SECTOR, 0x34000)
        with tempfile.TemporaryDirectory() as folder:
            again = quiet(run, 'install', Path(folder), Recorder(), link=device)
        self.assertEqual(again['writes'], 0)
        with tempfile.TemporaryDirectory() as folder:
            quiet(run, 'restore', Path(folder), Recorder(), link=device)
        self.assertEqual(bytes(device.flash), self.stock)


if __name__ == '__main__':
    unittest.main()



@unittest.skipUnless(TESTER.exists(), 'tester backup unavailable')
class IineLayoutTests(unittest.TestCase):
    """A user's unit on IINE's current flash layout with the Sep 2025 factory build."""

    @classmethod
    def setUpClass(cls):
        cls.image = TESTER.read_bytes()
        cls.oem = official_application()

    def test_tester_unit_is_recognised(self):
        self.assertEqual(classify(self.image), 'factory-2025-09')
        layout = Layout(self.image)
        self.assertEqual((layout.base, layout.limit, layout.key), (0x2000, 0x34000, 0x190d))
        self.assertEqual(layout.storage, [(0x34000, 0x7b000), (0x7b000, 0x7c000), (0x7c000, 0x7d000),
                                          (0x7d000, 0x7f000), (0x7f000, 0x80000)])

    def test_plans_keep_everything_outside_the_application_area(self):
        install = plan(self.image, self.oem, 'install')
        target = install['target']
        self.assertEqual((install['state'], classify(target)), ('factory-2025-09', 'custom'))
        self.assertEqual(Layout(target).application(), glassly_application())
        self.assertEqual(target[:0x2000], self.image[:0x2000])
        self.assertEqual(target[0x34000:], self.image[0x34000:])
        self.assertTrue(install['changed_sectors'] and all(0x2000 <= a < 0x34000 for a in install['changed_sectors']))
        self.assertEqual(plan(target, self.oem, 'install')['changed_sectors'], [])
        restore = plan(target, self.oem, 'restore')
        self.assertEqual(classify(restore['target']), 'stock')
        self.assertEqual(Layout(restore['target']).application(), self.oem)

    def test_run_installs_and_restores_on_the_iine_layout(self):
        device = FakeBD19(self.image, absent_polls=1)
        with tempfile.TemporaryDirectory() as folder:
            report = quiet(run, 'install', Path(folder), Recorder(), link=device)
        self.assertEqual(report['state'], 'factory-2025-09')
        self.assertTrue(report['full_readback_verified'])
        self.assertEqual(classify(bytes(device.flash)), 'custom')
        self.assertEqual(bytes(device.flash[0x34000:]), self.image[0x34000:])
        self.assertEqual(device.erased[-1], 0x2000)
        self.assertLessEqual(max(device.erased) + SECTOR, 0x34000)
        with tempfile.TemporaryDirectory() as folder:
            quiet(run, 'restore', Path(folder), Recorder(), link=device)
        self.assertEqual(classify(bytes(device.flash)), 'stock')
        self.assertEqual(bytes(device.flash[0x34000:]), self.image[0x34000:])
