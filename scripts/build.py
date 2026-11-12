"""Build the release download for the current OS into dist/release/."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
NAME = 'Glassly CFW Flasher'


def run(*command):
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)


def main():
    out = ROOT / 'dist/release'
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    if sys.platform == 'darwin':
        run('clang', '-fobjc-arc', '-dynamiclib', '-mmacosx-version-min=12.0', '-framework', 'Foundation',
            '-framework', 'IOKit', '-framework', 'IOUSBHost', 'native/jl_usb.m', '-o', 'native/libjl_usb.dylib')
        run('clang', '-fobjc-arc', '-dynamiclib', '-mmacosx-version-min=12.0', '-framework', 'Foundation',
            '-framework', 'IOBluetooth', '-framework', 'CoreBluetooth', 'native/memomind_rfcomm.m',
            '-o', 'native/libmemomind_rfcomm.dylib')
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', 'dist/pyinstaller',
        '--workpath', 'build/pyinstaller', 'packaging/glassly-cfw-flasher.spec')
    built = ROOT / 'dist/pyinstaller'
    machine = platform.machine().lower()
    if sys.platform == 'darwin':
        app = built / f'{NAME}.app'
        run('codesign', '--force', '--deep', '--sign', '-', app)
        staging = ROOT / 'build/dmg'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.copytree(app, staging / app.name, symlinks=True)
        os.symlink('/Applications', staging / 'Applications')
        label = 'AppleSilicon' if machine == 'arm64' else 'Intel'
        artifact = out / f'Glassly-CFW-Flasher-macOS-{label}.dmg'
        run('hdiutil', 'create', '-volname', NAME, '-srcfolder', staging, '-ov', '-format', 'UDZO', artifact)
    elif sys.platform == 'win32':
        artifact = out / 'Glassly-CFW-Flasher-Windows-x64.exe'
        shutil.copy2(built / 'Glassly-CFW-Flasher.exe', artifact)
    else:
        artifact = out / f'Glassly-CFW-Flasher-Linux-{"arm64" if machine == "aarch64" else "x86_64"}'
        shutil.copy2(built / 'Glassly-CFW-Flasher', artifact)
        artifact.chmod(0o755)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    print(f'{digest}  {artifact.name}')


if __name__ == '__main__':
    main()
