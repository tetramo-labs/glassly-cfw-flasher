import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys

from .devices.base import FlashError

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
APP_NAME = 'Glassly CFW Flasher'


def resource(relative):
    return ROOT / relative


def firmware(relative):
    """Bytes of a bundled firmware file, checked against its directory's manifest.json."""
    path = PurePosixPath(relative)
    folder = resource('firmware') / path.parts[0]
    entry = json.loads((folder / 'manifest.json').read_text())['files'].get(path.name)
    if len(path.parts) != 2 or entry is None:
        raise FlashError(f'{relative} is not a bundled firmware file')
    data = (folder / path.name).read_bytes()
    if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise FlashError(f'Bundled firmware {relative} is damaged; download the flasher again')
    return data


def backups_dir():
    if sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support' / APP_NAME
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / APP_NAME
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'glassly-cfw-flasher'
    return base / 'backups'
