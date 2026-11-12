# PyInstaller spec for the release builds; run through scripts/build.py.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from glassly_flasher import __version__  # noqa: E402

NAME = 'Glassly CFW Flasher'
VENDORED = [ROOT / 'third_party/jl-misctools/firmware', ROOT / 'third_party/jl-uboot-tool']

datas = [(str(ROOT / 'firmware'), 'firmware'),
         (str(ROOT / 'third_party/jl-uboot-tool/data'), 'third_party/jl-uboot-tool/data')]
datas += [(str(p), str(p.parent.relative_to(ROOT))) for p in (ROOT / 'glassly_flasher').rglob('*.json')]
binaries = [(str(ROOT / 'native/libjl_usb.dylib'), 'native')] if sys.platform == 'darwin' else []
hidden = ['hidraw' if sys.platform.startswith('linux') else 'hid']
hidden += ['glassly_flasher.devices.' + m for m in ('eightbitdo.zero2', 'eightbitdo.micro', 'gamebrick', 'ananke')]
hidden += ['jltech', 'jltech.cipher', 'jltech.crc', 'jltech.chipkeybin']
# win32port loads WinDLL at import, so each OS only gets its own scsiio backend.
if sys.platform == 'win32':
    hidden += ['scsiio', 'scsiio.common', 'scsiio.win32port']
elif sys.platform.startswith('linux'):
    hidden += ['scsiio', 'scsiio.common', 'scsiio.linuxport']

a = Analysis([str(ROOT / 'packaging/launch.py')], pathex=[str(ROOT), *map(str, VENDORED)], binaries=binaries,
             datas=datas, hiddenimports=hidden, excludes=['unittest', 'pydoc', 'pyflakes'])
pyz = PYZ(a.pure)
icon = str(ROOT / 'packaging/icon.png')

if sys.platform == 'darwin':
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, icon=icon)
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME)
    app = BUNDLE(coll, name=f'{NAME}.app', icon=icon, bundle_identifier='com.tetramo.glassly-cfw-flasher',
                 version=__version__, info_plist={'CFBundleShortVersionString': __version__,
                                                  'NSHighResolutionCapable': True,
                                                  'LSMinimumSystemVersion': '12.0'})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME.replace(' ', '-'), console=False, icon=icon,
              uac_admin=sys.platform == 'win32')
