import argparse
import json
import os
from pathlib import Path
import sys
import traceback

from .devices import DEVICES
from .devices.base import Cancelled, FlashError
from .report import EventReporter, LineWriter


def give_back(folder, uid):
    if uid is None or sys.platform == 'win32' or os.geteuid() != 0:
        return
    for path in [folder, *folder.rglob('*')]:
        os.chown(path, uid, -1)


def main(argv):
    parser = argparse.ArgumentParser(prog='glassly-cfw-flasher --worker')
    parser.add_argument('device', choices=sorted(DEVICES))
    parser.add_argument('mode', choices=['install', 'restore', 'backup'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--events', type=Path, required=True)
    parser.add_argument('--cancel', type=Path)
    parser.add_argument('--owner', type=int)
    args = parser.parse_args(argv)
    device = DEVICES[args.device]
    reporter = EventReporter(args.events, args.output / 'operation.log', args.cancel)
    sys.stdout = sys.stderr = LineWriter(reporter.log)
    code = 1
    try:
        if args.mode not in device.modes:
            raise FlashError(f'{device.name} does not support {args.mode}')
        reporter.log(f'{device.name}: {args.mode}. Backups and logs: {args.output}')
        summary = device.run(args.mode, args.output, reporter)
        (args.output / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        reporter.emit(type='result', ok=True, summary=summary)
        code = 0
    except Cancelled as e:
        reporter.emit(type='result', ok=False, cancelled=True, message=str(e))
    except FlashError as e:
        reporter.log(f'Stopped: {e}')
        reporter.emit(type='result', ok=False, message=str(e))
    except BaseException as e:
        reporter.log(traceback.format_exc())
        reporter.emit(type='result', ok=False, message=f'Unexpected error: {e}. Keep the backup folder and report this.')
    finally:
        reporter.close()
        give_back(args.output, args.owner)
    return code
