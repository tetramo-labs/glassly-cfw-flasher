"""Run the built app's worker for every controller with cancel pre-set: proves the bundle imports and
reads its firmware, enumerates USB, and stops before touching anything."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from glassly_flasher.devices import DEVICES  # noqa: E402


def executable():
    built = ROOT / 'dist/pyinstaller'
    if sys.platform == 'darwin':
        return built / 'Glassly CFW Flasher.app/Contents/MacOS/Glassly CFW Flasher'
    return built / ('Glassly-CFW-Flasher.exe' if sys.platform == 'win32' else 'Glassly-CFW-Flasher')


def main():
    failed = False
    for key in DEVICES:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            output, events, cancel = folder / 'out', folder / 'events.jsonl', folder / 'cancel'
            output.mkdir()
            events.touch()
            cancel.touch()
            subprocess.run([str(executable()), '--worker', key, 'install', '--output', str(output),
                            '--events', str(events), '--cancel', str(cancel)], timeout=120)
            lines = events.read_text().splitlines()
            result = json.loads(lines[-1]) if lines else {}
            ok = result.get('cancelled') is True
            failed |= not ok
            print(f'{key}: {"ok" if ok else "FAILED"} {result}')
            if not ok:
                print('\n'.join(lines))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
