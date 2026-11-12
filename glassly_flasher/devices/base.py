from dataclasses import dataclass
import importlib


class FlashError(Exception):
    pass


class Cancelled(FlashError):
    pass


class Reporter:
    def log(self, text):
        raise NotImplementedError

    def status(self, text):
        raise NotImplementedError

    def progress(self, done, total):
        raise NotImplementedError

    def cancelled(self):
        return False

    # Only call while nothing has been written; a cancel mid-write must never abort the flash.
    def check_cancel(self):
        if self.cancelled():
            raise Cancelled('Cancelled. Nothing was written to the controller.')


@dataclass(frozen=True)
class Device:
    key: str
    name: str
    supported: str
    update_mode: str
    after_install: str
    after_restore: str
    module: str
    elevate: frozenset
    tested: str
    modes: tuple = ('install', 'restore', 'backup')

    def run(self, mode, output, reporter):
        return importlib.import_module(self.module).run(mode, output, reporter)
