"""Shared host side of the Jieli USB update mode (Gamebrick Mini: BD19, Ananke Gen 2: BR23)."""
import os
import sys
import threading
import time

from ...paths import resource
from ..base import FlashError

for _relative in ('third_party/jl-misctools/firmware', 'third_party/jl-uboot-tool'):
    _path = str(resource(_relative))
    if _path not in sys.path:
        sys.path.append(_path)

WAIT_SECONDS = 600


def loader_blob(name):
    return resource('third_party/jl-uboot-tool/data/loaderblobs/usb/' + name).read_bytes()


def wait_for_device(link, reporter, seconds=WAIT_SECONDS, poll=0.5, clock=time.monotonic, sleep=time.sleep):
    reporter.status('Waiting for the controller in update mode…')
    deadline = clock() + seconds
    while True:
        reporter.check_cancel()
        if link.try_open():
            return link
        if clock() > deadline:
            raise FlashError('No controller in update mode appeared within 10 minutes. Nothing was written.')
        sleep(poll)


class Watchdog:
    """Hard bound on one USB session. Expiry ends the worker process; there is no retry."""

    def __init__(self, seconds, exit=os._exit):
        self.seconds = seconds
        self.exit = exit
        self.timer = threading.Timer(seconds, self.expire)
        self.timer.daemon = True

    def expire(self):
        print(f'Watchdog: the USB session exceeded {self.seconds} seconds; stopping.', flush=True)
        self.exit(124)

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *exc):
        self.timer.cancel()
        return False
