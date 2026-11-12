import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time


def follow(events, done, console):
    offset = 0
    while not done.is_set() or events.stat().st_size > offset:
        with events.open('rb') as f:
            f.seek(offset)
            chunk = f.read()
        offset += len(chunk)
        for line in chunk.decode('utf-8', 'replace').splitlines():
            event = json.loads(line)
            if event['type'] == 'log':
                console.write(event['text'] + '\n')
            elif event['type'] == 'result' and event['ok']:
                console.write('DONE ' + json.dumps(event['summary']) + '\n')
        console.flush()
        time.sleep(0.1)


def cli(argv):
    from . import paths
    from .devices import DEVICES
    from .worker import main as worker

    parser = argparse.ArgumentParser(prog='glassly-cfw-flasher --cli',
                                     description='Flash from a terminal. IINE controllers, and every controller on '
                                                 'Linux, need sudo (Administrator on Windows).')
    parser.add_argument('device', choices=sorted(DEVICES))
    parser.add_argument('mode', choices=['install', 'restore', 'backup'])
    parser.add_argument('--i-accept-the-risk', action='store_true', help='required for install and restore')
    args = parser.parse_args(argv)
    device = DEVICES[args.device]
    if args.mode not in device.modes:
        parser.error(f'{device.name} supports: {", ".join(device.modes)}')
    if args.mode != 'backup' and not args.i_accept_the_risk:
        parser.error('experimental firmware can leave the controller unusable; pass --i-accept-the-risk')
    output = paths.backups_dir() / f'{device.key}-{args.mode}-{time.strftime("%Y%m%d-%H%M%S")}'
    output.mkdir(parents=True)
    events = Path(tempfile.mkdtemp(prefix='glassly-flasher-')) / 'events.jsonl'
    events.touch()
    console, done = sys.stdout, threading.Event()
    console.write(device.update_mode + '\n')
    thread = threading.Thread(target=follow, args=(events, done, console))
    thread.start()
    try:
        return worker([device.key, args.mode, '--output', str(output), '--events', str(events)])
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        done.set()
        thread.join()


def main():
    argv = sys.argv[1:]
    if argv[:1] == ['--worker']:
        from .worker import main as worker
        sys.exit(worker(argv[1:]))
    if argv[:1] == ['--cli']:
        sys.exit(cli(argv[1:]))
    from .gui import main as gui
    gui()


if __name__ == '__main__':
    main()
