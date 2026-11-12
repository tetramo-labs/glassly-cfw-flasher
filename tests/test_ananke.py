"""Ananke Gen 2 residue gates, sector writer, plans and run() against fake devices. No USB access.

Tests marked with FIXTURES use the unit backup in a local glassly-ananke-cfw checkout and skip
when it is absent."""
import binascii
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from glassly_flasher.devices.ananke import run
from glassly_flasher.devices.ananke.image import (AREA_BYTES, BASE, CUSTOM_SHA, OEM_SHA, applications, digest, layout,
                                                   plan)
from glassly_flasher.devices.ananke.link import (BR23_INQUIRY, LOADER_QUERY_POLICY, PAGE, ROM_RAM_POLICY, SECTOR,
                                                  SECTORS, SIZE, Br23BulkOnlyLink, Br23PassThroughLink)
from glassly_flasher.devices.ananke.transport import Transport, Writer, changed_sectors, read_range
from glassly_flasher.devices.base import FlashError, Reporter
from glassly_flasher.devices.jieli import loader_blob
from glassly_flasher.devices.jieli.link import INQUIRY, LinkError

ANANKE = Path('/Users/fosse/dev/glassly-ananke-cfw')
ORIGINAL = ANANKE/'research/device-backup-20261103-174931/flash-pass1.bin'
FIXTURES = unittest.skipUnless(ORIGINAL.exists(), 'Ananke unit backup unavailable')
VERIFIED_INSTALL_SHA = '574f6542ef749741721d8bf040aa0b45eb8837d9357b900865473400027d2c59'
IDENTITY = BR23_INQUIRY
LOADER = loader_blob('br23loader.bin')
QUERY_REPLIES = {0xfc0a: bytes.fromhex('030014608500'), 0xfc0b: bytes.fromhex('00856000'),
                 0xfc14: bytes.fromhex('00008000')}


def residue_for(op, size):
    if op in (0xfb06, 0xfd07):
        return 512
    if op in (0xfb08, 0xfc0a, 0xfc0b, 0xfc14, 0xfb01):
        return 16
    if op in (0xfd05, 0xfb04):
        return size
    return 0


class FakeBr23(Br23BulkOnlyLink):
    """BR23 ROM + pinned loader + 1 MiB NOR behind Bulk-Only framing, with the observed residues."""

    def __init__(self, memory=bytes(SIZE)):
        super().__init__(None)
        self.memory = bytearray(memory)
        self.ram = bytearray(len(LOADER))
        self.calls = []
        self.bad_status = None
        self.bad_ram = False
        self.ignore_erase = False
        self.corrupt_write = False
        self.erased = []

    def try_open(self):
        self.opened = True
        return True

    def close(self):
        self.opened = False

    def transfer(self, ep, value):
        if ep == 1 and len(value) == 31 and value[:4] == b'USBC':
            _, self.current_tag, self.size, _, _, _, self.cdb = struct.unpack('<4sIIBBB16s', value)
            self.calls.append(self.cdb)
            self.op = int.from_bytes(self.cdb[:2], 'big')
            self.address = int.from_bytes(self.cdb[2:6], 'big')
            if self.op == 0xfb01:
                self.erased.append(self.address)
                if not self.ignore_erase:
                    self.memory[self.address:self.address+SECTOR] = b'\xff'*SECTOR
            return b''
        if ep == 1:
            if self.op == 0xfb06:
                offset = self.address - 0x12000
                self.ram[offset:offset+len(value)] = value
            else:
                for i, b in enumerate(value):
                    self.memory[self.address+i] &= b
                if self.corrupt_write:
                    self.memory[self.address] ^= 1
            return b''
        if value == 13:
            fields = dict(signature=b'USBS', tag=self.current_tag, residue=residue_for(self.op, self.size), status=0)
            fields.update(self.bad_status or {})
            return struct.pack('<4sIIB', *fields.values())
        if self.cdb[:6] == INQUIRY:
            return IDENTITY[:value]
        if self.op == 0xfd07:
            offset = self.address - 0x12000
            return bytes(512) if self.bad_ram else bytes(self.ram[offset:offset+value])
        if self.op == 0xfd05:
            return bytes(self.memory[self.address:self.address+value])
        return (self.cdb[:2] + QUERY_REPLIES.get(self.op, b'')).ljust(value, b'\0')

    def writes(self):
        return [c for c in self.calls if c[:2] in (b'\xfb\x01', b'\xfb\x04')]


class FakeBr23SCSIDev:
    """The same device seen through the kernel pass-through: execute(cdb, data_out, data_in) -> residue."""

    def __init__(self, fake, hide_residue=False):
        self.fake = fake
        self.hide_residue = hide_residue

    def __call__(self):
        return self

    def open(self, path):
        pass

    def close(self):
        pass

    def execute(self, cdb, data_out, data_in):
        size = len(data_out) if data_out is not None else len(data_in) if data_in is not None else 0
        cbw = struct.pack('<4sIIBBB16s', b'USBC', 1, size, 0x80 if data_in is not None else 0, 0, len(cdb),
                          bytes(cdb).ljust(16, b'\0'))
        self.fake.transfer(1, cbw)
        if data_out is not None:
            self.fake.transfer(1, data_out)
        elif data_in is not None:
            data_in[:] = self.fake.transfer(0x81, len(data_in))
        residue = struct.unpack('<4sIIB', self.fake.transfer(0x81, 13))[2]
        return 0 if self.hide_residue else residue


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


def write_cdb():
    return (bytes.fromhex('fb0600012000020000') + binascii.crc_hqx(LOADER[:512], 0).to_bytes(2, 'little') + b'\xff'*5)


class RomResidueTests(unittest.TestCase):
    def test_full_pinned_upload_readback_and_strict_post_loader(self):
        link = FakeBr23()
        records = []
        quiet(Transport(link).prepare, IDENTITY, LOADER, records.append)
        self.assertIsNone(link.rom_loader)
        self.assertEqual(records[0]['rom_ram_residue_policy'], ROM_RAM_POLICY)
        self.assertEqual(len(records[1]['ram_commands']), 44)
        self.assertTrue(all(c['expected_residue'] == c['residue'] == 512 for c in records[1]['ram_commands']))
        self.assertEqual(link.last_command['residue_policy'], LOADER_QUERY_POLICY)
        self.assertEqual(link.expected_residue(bytes.fromhex('fd05000000000100')+b'\xff'*8, None, 256), 256)
        self.assertEqual(link.flash_read_size, SIZE)

    def test_flash_profile_requires_measured_identity_and_verified_session(self):
        link = FakeBr23()
        with self.assertRaises(LinkError):
            link.enable_br23_flash_reads(SIZE)
        quiet(Transport(link).prepare, IDENTITY, LOADER)
        for size in (0, 0x80000, 0x200000):
            with self.assertRaises(LinkError):
                link.enable_br23_flash_reads(size)
        link.loader_query_replies[0xfc0b] = b'\xfc\x0b'+bytes(14)
        with self.assertRaises(LinkError):
            link.enable_br23_flash_reads(SIZE)

    def test_flash_read_bounds_and_status(self):
        link = FakeBr23(bytes(range(256)) * 4096)
        transport = Transport(link)
        quiet(transport.prepare, IDENTITY, LOADER)
        self.assertEqual(transport.command(0xfd05, struct.pack('>IH', 0, 256), n=256), bytes(range(256)))
        count = len(link.calls)
        for address, n in ((1, 256), (SIZE, 256), (0, 512)):
            with self.assertRaises(LinkError):
                link.execute(b'\xfd\x05'+struct.pack('>IH', address, n)+b'\xff'*8, n=n)
        self.assertEqual(len(link.calls), count)
        for bad in ({'residue': 0}, {'residue': 255}, {'residue': 257}, {'status': 1}, {'tag': 99}):
            link.bad_status = bad
            with self.assertRaises(LinkError):
                transport.command(0xfd05, struct.pack('>IH', 0, 256), n=256)

    def test_loader_query_profile_remains_strict(self):
        for bad in ({'residue': 0}, {'residue': 1}, {'residue': 15}, {'residue': 17},
                    {'status': 1}, {'tag': 99}, {'signature': b'bad!'}):
            link = FakeBr23()
            quiet(Transport(link).prepare, IDENTITY, LOADER)
            link.bad_status = bad
            with self.assertRaises(LinkError):
                link.execute(b'\xfc\x0a'+b'\xff'*14, n=16)
        link = FakeBr23()
        quiet(Transport(link).prepare, IDENTITY, LOADER)
        count = len(link.calls)
        for cdb, out, n in ((b'\xfc\x0a'+bytes(14), None, 16), (b'\xfc\x0b'+b'\xff'*14, None, 15),
                            (b'\xfc\x14'+b'\xff'*14, bytes(16), 0)):
            with self.assertRaises(LinkError):
                link.execute(cdb, out, n)
        self.assertEqual(len(link.calls), count)

    def test_identity_and_blob_gate(self):
        for identity, loader in ((IDENTITY.replace(b'BR23', b'BD19'), LOADER), (IDENTITY[:-1]+b'1', LOADER),
                                 (IDENTITY, bytes(len(LOADER)))):
            link = FakeBr23()
            with self.assertRaises(LinkError):
                link.enable_br23_rom_ram(identity, loader)
            self.assertIsNone(link.rom_loader)
            self.assertEqual(link.calls, [])

    def test_arbitrary_residue_status_tag_signature_still_fail(self):
        for bad in ({'residue': 0}, {'residue': 1}, {'residue': 511}, {'residue': 513},
                    {'status': 1}, {'status': 2}, {'tag': 99}, {'signature': b'bad!'}):
            with self.subTest(bad=bad):
                link = FakeBr23()
                link.bad_status = bad
                link.enable_br23_rom_ram(IDENTITY, LOADER)
                with self.assertRaises(LinkError):
                    link.execute(write_cdb(), out=LOADER[:512])
                self.assertFalse(link.last_command['complete'])

    def test_ram_mismatch_never_reaches_jump(self):
        link = FakeBr23()
        link.bad_ram = True
        with self.assertRaisesRegex(RuntimeError, 'RAM readback mismatch'):
            quiet(Transport(link).prepare, IDENTITY, LOADER)
        self.assertEqual([c[:2].hex() for c in link.calls], ['fb06', 'fd07'])

    def test_jump_requires_all_blocks_and_exact_arguments(self):
        link = FakeBr23()
        link.enable_br23_rom_ram(IDENTITY, LOADER)
        jump = bytes.fromhex('fb08000120000001') + b'\xff'*8
        with self.assertRaises(LinkError):
            link.execute(jump, n=16)
        self.assertEqual(link.calls, [])
        link.rom_verified = set(range(0, 11264, 512))
        with self.assertRaises(LinkError):
            link.execute(jump[:7] + b'\x07' + jump[8:], n=16)
        self.assertEqual(link.calls, [])
        link.execute(jump, n=16)
        self.assertEqual(link.last_command['expected_residue'], 16)
        self.assertIsNone(link.rom_loader)

    def test_bad_acknowledgement_stops_before_flash_queries(self):
        class BadJump(FakeBr23):
            def transfer(self, ep, value):
                data = super().transfer(ep, value)
                if self.cdb[:2] == b'\xfb\x08' and ep == 0x81 and value == 16:
                    return b'bad!' + data[4:]
                return data
        link = BadJump()
        with self.assertRaisesRegex(LinkError, 'acknowledgement mismatch'):
            quiet(Transport(link).prepare, IDENTITY, LOADER)
        self.assertNotIn(b'\xfc\x0a', [c[:2] for c in link.calls])

    def test_unpinned_ram_or_malformed_bounds_never_sent(self):
        for cdb, out, n in ((write_cdb(), bytes(512), 0), (write_cdb()[:5]+b'\x01'+write_cdb()[6:], LOADER[:512], 0),
                            (bytes.fromhex('fd0700014c000200')+b'\xff'*8, None, 512),
                            (bytes.fromhex('fd07000120000100')+b'\xff'*8, None, 256)):
            link = FakeBr23()
            link.enable_br23_rom_ram(IDENTITY, LOADER)
            with self.assertRaises(LinkError):
                link.execute(cdb, out=out, n=n)
            self.assertEqual(link.calls, [])


class WriterTests(unittest.TestCase):
    def reader(self, link):
        reader = Transport(link)
        reader.loader = b'test fixture: loader already verified'
        reader.size = SIZE
        return reader

    def setup_writer(self):
        before = bytes([0x55])*SIZE
        after = bytearray(before)
        after[0x4000:0x5000] = bytes(range(256))*16
        link = FakeBr23(before)
        link.flash_read_size = SIZE
        events = []
        writer = Writer(link, self.reader(link), before, bytes(after), events.append)
        return link, writer, events, bytes(after)

    def test_full_sector_write_verifies_and_preserves_surroundings(self):
        link, writer, events, after = self.setup_writer()
        writer.sector(0x4000)
        self.assertEqual(link.memory, after)
        self.assertEqual(sum(c[:2] == b'\xfb\x01' for c in link.calls), 1)
        self.assertEqual(sum(c[:2] == b'\xfb\x04' for c in link.calls), 16)
        self.assertEqual(events[-1]['event'], 'sector-verified')
        self.assertIsNone(link.pending_write)
        with self.assertRaisesRegex(RuntimeError, 'repeated sector'):
            writer.sector(0x4000)

    def test_wrong_preimage_stops_before_erase(self):
        link, writer, _, _ = self.setup_writer()
        link.memory[0x4000] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'changed since preflight'):
            writer.sector(0x4000)
        self.assertEqual(link.writes(), [])

    def test_erase_failure_never_programs(self):
        link, writer, _, _ = self.setup_writer()
        link.ignore_erase = True
        with self.assertRaisesRegex(RuntimeError, 'did not erase'):
            writer.sector(0x4000)
        self.assertFalse(any(c[:2] == b'\xfb\x04' for c in link.calls))

    def test_program_mismatch_stops_after_first_page(self):
        link, writer, _, _ = self.setup_writer()
        link.corrupt_write = True
        with self.assertRaisesRegex(RuntimeError, 'Program readback mismatch'):
            writer.sector(0x4000)
        self.assertEqual(sum(c[:2] == b'\xfb\x04' for c in link.calls), 1)

    def test_invalid_status_never_accepted(self):
        for bad in ({'status': 1}, {'residue': 0}, {'residue': 255}, {'tag': 99}, {'signature': b'nope'}):
            link, _, _, _ = self.setup_writer()
            link.bad_status = bad
            with self.assertRaises(LinkError):
                read_range(self.reader(link), 0, PAGE)
            self.assertFalse(link.last_command['complete'])

    def test_forbidden_commands_cannot_reach_usb(self):
        link, _, _, _ = self.setup_writer()
        for op in (0xfb00, 0xfb02, 0xfc03, 0xfc0c, 0xfc0d, 0xfc12):
            with self.assertRaises(LinkError):
                link.execute(op.to_bytes(2, 'big')+b'\xff'*14, n=16)
        for address in (0, 0x2000, 0x3001, 0x57000, 0xff000):
            cdb = b'\xfb\x01'+struct.pack('>I', address)+b'\xff'*10
            link.pending_write = (cdb, None, 16)
            with self.assertRaises(LinkError):
                link.execute(cdb, n=16)
        self.assertEqual(link.calls, [])

    def test_unarmed_write_and_read_transport_reject_mutation(self):
        link, _, _, _ = self.setup_writer()
        cdb = b'\xfb\x01'+struct.pack('>I', 0x4000)+b'\xff'*10
        with self.assertRaises(LinkError):
            link.execute(cdb, n=16)
        reader = Transport(link)
        reader.loader = b'test'
        with self.assertRaisesRegex(RuntimeError, 'Forbidden command'):
            reader.execute(cdb, n=16)
        self.assertEqual(link.calls, [])

    def test_changes_outside_application_sectors_are_refused(self):
        before = bytes(SIZE)
        desired = bytearray(before)
        desired[0xff000] = 1
        with self.assertRaisesRegex(RuntimeError, 'outside allowed'):
            changed_sectors(before, bytes(desired))
        self.assertEqual(SECTORS[-1], 0x3000, 'header sector last')


class BundleTests(unittest.TestCase):
    def test_applications_and_layout_are_pinned(self):
        stock, custom = applications()
        self.assertEqual(digest(stock[:338712]), OEM_SHA)
        self.assertEqual(stock[338712:], b'\xff' * (len(stock) - 338712))
        self.assertEqual(digest(custom), CUSTOM_SHA)
        self.assertEqual(len(layout()['application_entries']), 6)
        evidence = ANANKE/'docs/evidence/installed-0601.json'
        if evidence.exists():
            source = json.loads(evidence.read_text())
            self.assertEqual(layout(), {k: source[k] for k in ('top_entries', 'application_area', 'application_entries')})

    def test_backup_run_keeps_two_immutable_reads_and_never_writes(self):
        image = bytes(range(256)) * 4096
        for link in (FakeBr23(image), None):
            with self.subTest(transport='bulk-only' if link else 'pass-through'):
                if link is None:
                    fake = FakeBr23(image)
                    link = Br23PassThroughLink(FakeBr23SCSIDev(fake), ['/dev/sg1'], 'linux')
                    writes = fake.writes
                else:
                    writes = link.writes
                with tempfile.TemporaryDirectory() as folder:
                    folder = Path(folder)
                    report = quiet(run, 'backup', folder, Recorder(), link=link)
                    self.assertEqual(report['sha256'], hashlib.sha256(image).hexdigest())
                    for name in ('before-pass1.bin', 'before-pass2.bin'):
                        self.assertEqual((folder/name).read_bytes(), image)
                        self.assertFalse((folder/name).stat().st_mode & 0o222)
                    meta = json.loads((folder/'metadata.json').read_text())
                    self.assertTrue(meta['complete'] and meta['backup_complete'])
                    self.assertFalse(meta['flash_write_attempted'])
                    self.assertTrue(json.loads((folder/'backup.json').read_text())['passes_match'])
                self.assertEqual(writes(), [])

    def test_install_on_unknown_flash_is_refused_before_any_write(self):
        link = FakeBr23(bytes(range(256)) * 4096)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FlashError, 'Invalid flash header|Unsupported|Unknown'):
                quiet(run, 'install', Path(folder), Recorder(), link=link)
            meta = json.loads((Path(folder)/'metadata.json').read_text())
            self.assertTrue(meta['backup_complete'])
            self.assertFalse(meta['flash_write_attempted'])
            self.assertIn('error', meta)
        self.assertEqual(link.writes(), [])

    def test_wrong_identity_uploads_nothing(self):
        class OtherChip(FakeBr23):
            def transfer(self, ep, value):
                if ep == 0x81 and value == 36 and self.cdb[:6] == INQUIRY:
                    return IDENTITY.replace(b'BR23', b'BR25')
                return super().transfer(ep, value)
        link = OtherChip()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(FlashError, 'Unexpected identity'):
                quiet(run, 'backup', Path(folder), Recorder(), link=link)
        self.assertEqual([c[:6] for c in link.calls], [INQUIRY])


@FIXTURES
class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stock, cls.custom = applications()
        cls.original = ORIGINAL.read_bytes()

    def test_reproduces_verified_flash_and_restores(self):
        desired = plan(self.original, self.stock, self.custom, 'install')
        self.assertEqual(digest(desired), VERIFIED_INSTALL_SHA)
        self.assertEqual(plan(desired, self.stock, self.custom, 'install'), desired)
        restored = plan(desired, self.stock, self.custom, 'restore')
        self.assertEqual(plan(restored, self.stock, self.custom, 'install'), desired)
        self.assertEqual(restored[:BASE], self.original[:BASE])
        self.assertEqual(restored[BASE+AREA_BYTES:], self.original[BASE+AREA_BYTES:])

    def test_preserves_live_settings_and_keys(self):
        live = bytearray(self.original)
        live[0xf3000:] = bytes((i * 13) % 256 for i in range(len(live)-0xf3000))
        desired = plan(bytes(live), self.stock, self.custom, 'install')
        self.assertEqual(desired[BASE+AREA_BYTES:], live[BASE+AREA_BYTES:])
        self.assertEqual(desired[:BASE], live[:BASE])

    def test_rejects_changes_in_map_boot_config_and_app(self):
        for offset in (0, 0x20, 0xc0, 0x20a8, BASE, 0x3140, 0x56de8):
            bad = bytearray(self.original)
            bad[offset] ^= 1
            with self.assertRaises((ValueError, RuntimeError)):
                plan(bytes(bad), self.stock, self.custom, 'install')
        with self.assertRaises(RuntimeError):
            plan(self.original[:-1], self.stock, self.custom, 'install')
        with self.assertRaises(RuntimeError):
            plan(self.original, self.stock, self.custom[:-1]+b'\0', 'install')

    def test_run_installs_then_restores_with_full_readback(self):
        for hide_residue in (None, False, True):
            with self.subTest(transport='bulk-only' if hide_residue is None else f'pass-through hide_residue={hide_residue}'):
                fake = FakeBr23(self.original)
                if hide_residue is None:
                    link = fake
                else:
                    link = Br23PassThroughLink(FakeBr23SCSIDev(fake, hide_residue), ['/dev/sg1'],
                                               'win32' if hide_residue else 'linux')
                with tempfile.TemporaryDirectory() as folder:
                    folder = Path(folder)
                    report = quiet(run, 'install', folder, Recorder(), link=link)
                    self.assertTrue(report['full_readback_verified'])
                    self.assertEqual(report['after_sha256'], VERIFIED_INSTALL_SHA)
                    meta = json.loads((folder/'metadata.json').read_text())
                    self.assertTrue(meta['complete'] and meta['full_readback_verified'])
                    events = [json.loads(line) for line in (folder/'commands.jsonl').read_text().splitlines()]
                    self.assertEqual(events[-1]['event'], 'sector-verified')
                    self.assertEqual(events[-1]['address'], '0x3000', 'header sector last')
                self.assertEqual(digest(bytes(fake.memory)), VERIFIED_INSTALL_SHA)
                self.assertEqual(fake.erased[-1], 0x3000)
                self.assertTrue(set(fake.erased) <= set(SECTORS))
                if hide_residue is None:
                    with tempfile.TemporaryDirectory() as folder:
                        quiet(run, 'restore', Path(folder), Recorder(), link=link)
                    self.assertEqual(bytes(fake.memory), plan(self.original, self.stock, self.custom, 'restore'))


if __name__ == '__main__':
    unittest.main()
