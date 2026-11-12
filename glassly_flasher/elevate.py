import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .devices.base import FlashError

ROOT = Path(__file__).resolve().parent.parent
PROMPT = 'Glassly CFW Flasher needs administrator access to talk to the controller over USB.'


def is_admin():
    if sys.platform == 'win32':
        import ctypes
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except OSError:
            return False
    return os.geteuid() == 0


def worker_command(args):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--worker', *args]
    return [sys.executable, '-m', 'glassly_flasher', '--worker', *args]


def applescript_string(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def launch(device, args):
    """Starts the worker, with administrator rights when this controller needs them on this OS."""
    command = worker_command(args)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if sys.platform not in device.elevate or is_admin():
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=flags)
    if sys.platform == 'darwin':
        shell = ' '.join(shlex.quote(part) for part in command) + ' >/dev/null 2>&1'
        script = (f'do shell script {applescript_string(shell)} with prompt {applescript_string(PROMPT)} '
                  'with administrator privileges')
        return subprocess.Popen(['osascript', '-e', script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    if sys.platform.startswith('linux'):
        pkexec = shutil.which('pkexec')
        if pkexec is None:
            raise FlashError('pkexec is not installed. Run the flasher with sudo instead.')
        if not getattr(sys, 'frozen', False):
            command = ['/usr/bin/env', f'PYTHONPATH={ROOT}', *command]
        return subprocess.Popen([pkexec, *command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    raise FlashError('Start the flasher as Administrator (right-click it and choose Run as administrator).')


def declined(stderr):
    """True when the user dismissed the password prompt."""
    return '-128' in stderr or 'Not authorized' in stderr or 'dismissed' in stderr.lower()
