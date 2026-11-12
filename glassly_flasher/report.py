import json
import time

from .devices.base import Reporter


class EventReporter(Reporter):
    """Writes progress as JSON lines for the GUI and a plain operation.log beside the backups."""

    def __init__(self, events, log, cancel=None):
        self.events = open(events, 'a', encoding='utf-8')
        self.log_file = open(log, 'a', encoding='utf-8')
        self.cancel = cancel
        self.last_progress = 0.0

    def emit(self, **event):
        self.events.write(json.dumps(event) + '\n')
        self.events.flush()

    def log(self, text):
        self.log_file.write(text + '\n')
        self.log_file.flush()
        self.emit(type='log', text=text)

    def status(self, text):
        self.log(text)
        self.emit(type='status', text=text)

    def progress(self, done, total):
        now = time.monotonic()
        if done == total or now - self.last_progress > 0.2:
            self.last_progress = now
            self.emit(type='progress', done=done, total=total)

    def cancelled(self):
        return self.cancel is not None and self.cancel.exists()

    def close(self):
        self.events.close()
        self.log_file.close()


class LineWriter:
    """File-like object that forwards complete lines of print() output to a callback."""

    def __init__(self, callback):
        self.callback = callback
        self.buffer = ''

    def write(self, text):
        self.buffer += text
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            if line.strip():
                self.callback(line.rstrip())
        return len(text)

    def flush(self):
        pass
