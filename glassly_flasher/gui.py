import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from . import __version__, elevate, paths
from .devices import DEVICES
from .devices.base import FlashError

MODES = {
    'install': 'Install the Glassly firmware',
    'restore': 'Put the official firmware back',
    'backup': 'Back up only (writes nothing)',
    'restore-backup': "Put back this controller's own backup (you choose its flash-pass1.bin)",
}
WRAP = 560


class App:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.temp = None
        self.output = None
        root.title('Glassly CFW Flasher')
        root.minsize(640, 720)
        root.protocol('WM_DELETE_WINDOW', self.close)
        bold = font.nametofont('TkDefaultFont').copy()
        bold.configure(weight='bold')
        self.bold = bold

        self.device = tk.StringVar(value=next(iter(DEVICES)))
        self.mode = tk.StringVar(value='install')
        self.accept = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='')
        self.result = tk.StringVar(value='')

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Glassly CFW Flasher', font=bold).pack(anchor='w')
        ttk.Label(frame, text=f'Version {__version__}. Installs Glassly button firmware on supported game controllers '
                              'so they work with the Glassly App.', wraplength=WRAP).pack(anchor='w', pady=(2, 10))

        box = ttk.LabelFrame(frame, text='1. Controller', padding=10)
        box.pack(fill='x')
        for key, device in DEVICES.items():
            ttk.Radiobutton(box, text=device.name, value=key, variable=self.device,
                            command=self.refresh).pack(anchor='w')
        self.supported = ttk.Label(box, wraplength=WRAP, foreground='#555')
        self.supported.pack(anchor='w', pady=(6, 0))

        box = ttk.LabelFrame(frame, text='2. What to do', padding=10)
        box.pack(fill='x', pady=(10, 0))
        self.mode_buttons = {key: ttk.Radiobutton(box, text=label, value=key, variable=self.mode, command=self.refresh)
                             for key, label in MODES.items()}
        for button in self.mode_buttons.values():
            button.pack(anchor='w')

        box = ttk.LabelFrame(frame, text='3. Put the controller in update mode', padding=10)
        box.pack(fill='x', pady=(10, 0))
        self.instructions = ttk.Label(box, wraplength=WRAP)
        self.instructions.pack(anchor='w')
        ttk.Label(box, text='Connect only the controller you are flashing. You can start the flasher first; it waits '
                            'for the controller.', wraplength=WRAP, foreground='#555').pack(anchor='w', pady=(6, 0))

        self.risk = ttk.Checkbutton(frame, variable=self.accept, command=self.refresh, text=(
            'I understand this firmware is experimental and a failed flash could leave the controller unusable.'))
        self.risk.pack(anchor='w', pady=(12, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(10, 0))
        self.start_button = ttk.Button(buttons, text='Start', command=self.start)
        self.start_button.pack(side='left')
        self.cancel_button = ttk.Button(buttons, text='Cancel', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left', padx=(8, 0))
        self.folder_button = ttk.Button(buttons, text='Open backup folder', command=self.open_folder, state='disabled')
        self.folder_button.pack(side='right')

        ttk.Label(frame, textvariable=self.status, font=bold).pack(anchor='w', pady=(12, 0))
        self.bar = ttk.Progressbar(frame, mode='determinate', maximum=1000)
        self.bar.pack(fill='x', pady=(4, 0))
        self.result_label = ttk.Label(frame, textvariable=self.result, wraplength=WRAP)
        self.result_label.pack(anchor='w', pady=(8, 0))

        log_frame = ttk.Frame(frame)
        log_frame.pack(fill='both', expand=True, pady=(8, 0))
        self.log = tk.Text(log_frame, height=10, wrap='word', state='disabled', relief='flat',
                           font=font.nametofont('TkFixedFont'))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.refresh()

    def selected(self):
        return DEVICES[self.device.get()]

    def running(self):
        return self.process is not None

    def refresh(self):
        device = self.selected()
        if self.mode.get() not in device.modes:
            self.mode.set('install')
        for key, button in self.mode_buttons.items():
            button.configure(state='normal' if key in device.modes and not self.running() else 'disabled')
        self.supported.configure(text=f'{device.supported}\n{device.tested}')
        self.instructions.configure(text=device.update_mode)
        writes = self.mode.get() != 'backup'
        self.risk.configure(state='normal' if writes and not self.running() else 'disabled')
        ready = not self.running() and (self.accept.get() or not writes)
        self.start_button.configure(state='normal' if ready else 'disabled')

    def append(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def start(self):
        device, mode = self.selected(), self.mode.get()
        self.output = paths.backups_dir() / f"{device.key}-{mode}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.output.mkdir(parents=True)
        self.temp = Path(tempfile.mkdtemp(prefix='glassly-flasher-'))
        self.events = self.temp / 'events.jsonl'
        self.events.touch()
        args = [device.key, mode, '--output', str(self.output), '--events', str(self.events),
                '--cancel', str(self.temp / 'cancel')]
        if mode == 'restore-backup':
            chosen = filedialog.askopenfilename(title='Choose flash-pass1.bin from an earlier session of this controller',
                                                initialdir=str(paths.backups_dir()),
                                                filetypes=[('Flash backup', 'flash-pass1.bin'), ('All files', '*')])
            if not chosen:
                self.output.rmdir()
                return
            args += ['--backup', chosen]
        if sys.platform != 'win32':
            args += ['--owner', str(os.getuid())]
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        self.result.set('')
        self.status.set('Starting')
        self.bar.configure(mode='indeterminate')
        self.bar.start(15)
        try:
            self.process = elevate.launch(device, args)
        except FlashError as e:
            self.bar.stop()
            self.status.set('Could not start')
            self.result.set(str(e))
            return
        self.offset, self.buffer, self.outcome = 0, '', None
        self.cancel_button.configure(state='normal')
        self.folder_button.configure(state='normal')
        self.refresh()
        self.root.after(100, self.poll)

    def poll(self):
        with self.events.open('rb') as f:
            f.seek(self.offset)
            data = f.read()
        self.offset += len(data)
        self.buffer += data.decode('utf-8', 'replace')
        *lines, self.buffer = self.buffer.split('\n')
        for line in lines:
            if line.strip():
                self.handle(json.loads(line))
        if self.process.poll() is None or lines:
            self.root.after(100, self.poll)
        else:
            self.finish()

    def handle(self, event):
        kind = event.get('type')
        if kind == 'log':
            self.append(event['text'])
        elif kind == 'status':
            self.status.set(event['text'])
            self.bar.configure(mode='indeterminate')
            self.bar.start(15)
        elif kind == 'progress':
            self.bar.stop()
            self.bar.configure(mode='determinate', value=1000 * event['done'] / max(event['total'], 1))
        elif kind == 'result':
            self.outcome = event

    def finish(self):
        process, self.process = self.process, None
        stderr = process.stderr.read().decode('utf-8', 'replace') if process.stderr else ''
        self.bar.stop()
        device, outcome = self.selected(), self.outcome
        if outcome is None:
            if elevate.declined(stderr) or (sys.platform.startswith('linux') and process.returncode in (126, 127)):
                self.status.set('Administrator access was not granted')
                self.result.set('Nothing was written. Start again and enter your password when asked.')
            else:
                self.status.set('The flasher stopped unexpectedly')
                self.result.set(f'Exit code {process.returncode}. Keep the backup folder and report this.')
                if stderr.strip():
                    self.append(stderr.strip())
            self.bar.configure(mode='determinate', value=0)
        elif outcome['ok']:
            self.bar.configure(mode='determinate', value=1000)
            mode = self.mode.get()
            self.status.set({'install': 'Glassly firmware installed', 'restore': 'Official firmware restored',
                             'backup': 'Backup complete', 'restore-backup': 'Backup restored'}[mode])
            self.result.set({'install': device.after_install, 'restore': device.after_restore,
                             'backup': 'Nothing was written. Unplug the controller and switch it off and on.',
                             'restore-backup': 'Unplug the controller and switch it off and on. It is back on the '
                                               'application from the backup.'}[mode])
        else:
            self.bar.configure(mode='determinate', value=0)
            self.status.set('Cancelled' if outcome.get('cancelled') else 'Stopped')
            self.result.set(outcome['message'])
        self.cancel_button.configure(state='disabled')
        self.refresh()

    def cancel(self):
        if self.temp is not None:
            (self.temp / 'cancel').touch()
            self.status.set('Cancelling if nothing has been written yet…')

    def open_folder(self):
        if self.output is None:
            return
        if sys.platform == 'darwin':
            subprocess.Popen(['open', str(self.output)])
        elif sys.platform == 'win32':
            os.startfile(self.output)
        else:
            subprocess.Popen(['xdg-open', str(self.output)])

    def close(self):
        if self.running():
            messagebox.showwarning('Glassly CFW Flasher', 'The flasher is still working. Closing it now could leave the '
                                   'controller unusable. Wait for it to finish, or press Cancel while it is waiting '
                                   'for the controller.')
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
