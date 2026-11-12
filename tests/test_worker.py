import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from glassly_flasher import paths
from glassly_flasher.devices import DEVICES
from glassly_flasher.devices.base import FlashError


class FirmwareTests(unittest.TestCase):
    def test_every_bundled_file_matches_its_manifest(self):
        for manifest in sorted((paths.ROOT / 'firmware').glob('*/manifest.json')):
            for name in json.loads(manifest.read_text())['files']:
                paths.firmware(f'{manifest.parent.name}/{name}')

    def test_unlisted_file_is_refused(self):
        with self.assertRaises(FlashError):
            paths.firmware('zero2/manifest.json')


class WorkerTests(unittest.TestCase):
    def test_cancel_while_waiting_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            output, events, cancel = folder / 'out', folder / 'events.jsonl', folder / 'cancel'
            output.mkdir()
            events.touch()
            worker = subprocess.Popen([sys.executable, '-m', 'glassly_flasher', '--worker', 'zero2', 'install',
                                       '--output', str(output), '--events', str(events), '--cancel', str(cancel)],
                                      cwd=paths.ROOT)
            deadline = time.monotonic() + 20
            while 'Waiting for the controller' not in events.read_text() and time.monotonic() < deadline:
                time.sleep(0.1)
            cancel.touch()
            self.assertEqual(worker.wait(timeout=20), 1)
            result = [json.loads(line) for line in events.read_text().splitlines()][-1]
            self.assertEqual(result['type'], 'result')
            self.assertTrue(result['cancelled'])
            self.assertEqual(sorted(p.name for p in output.iterdir()), ['operation.log'])

    def test_registry_is_complete(self):
        self.assertEqual(set(DEVICES), {'zero2', 'micro', 'gamebrick', 'ananke'})
        for device in DEVICES.values():
            self.assertTrue({'install', 'restore'} <= set(device.modes))


if __name__ == '__main__':
    unittest.main()
