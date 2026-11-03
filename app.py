#!/usr/bin/env python3
"""Installable desktop interface for Sony gyro stabilization."""

from collections import deque
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parent
LEVELS = ('light', 'balanced', 'strong')
PROGRESS = re.compile(r'^\s*(tracking|visual cleanup|rendering)\s+(\d+)/(\d+)')
STAGES = {'tracking': 'Checking camera motion',
          'visual cleanup': 'Refining stabilization',
          'rendering': 'Rendering video'}


def suggested_output(source, level='balanced'):
    source = Path(source)
    return str(source.with_name(f'{source.stem}_stabilized_{level}.mov'))


def matching_xml(source):
    """Find the sidecar name Sony writes beside an original clip."""
    path = Path(source)
    return next((candidate for candidate in (
        path.with_name(path.stem + 'M01.XML'),
        path.with_name(path.stem + 'M01.xml'),
    ) if candidate.is_file()), None)


def validate_request(source, output, level, xml=None):
    if not isinstance(level, str) or level not in LEVELS:
        raise ValueError('Choose Light, Balanced, or Strong stabilization.')
    if not isinstance(source, str) or not source.strip():
        raise ValueError('Choose the original Sony video.')
    if not isinstance(output, str) or not output.strip():
        raise ValueError('Choose where to save the stabilized video.')
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError('The original video was not found.')
    if source_path.suffix.lower() not in ('.mp4', '.mov'):
        raise ValueError('Choose an original MP4 or MOV video.')
    if output_path.suffix.lower() not in ('.mp4', '.mov'):
        raise ValueError('Save the video with a .mov or .mp4 extension. MOV is recommended for Sony audio.')
    if source_path == output_path:
        raise ValueError('The output must have a different name from the original video.')
    if not output_path.parent.is_dir():
        raise ValueError('The destination folder does not exist.')
    if output_path.exists():
        raise ValueError('That output file already exists. Choose another filename.')
    xml_path = None
    if xml:
        if not isinstance(xml, str):
            raise ValueError('Choose a valid Sony XML file.')
        xml_path = Path(xml).expanduser().resolve()
        if not xml_path.is_file() or xml_path.suffix.lower() != '.xml':
            raise ValueError('The selected XML file was not found or is not an .xml file.')
    return source_path, output_path, xml_path


def batch_requests(sources, level, first_output='', first_xml=''):
    """Prepare one safe export per selected original."""
    if not sources:
        raise ValueError('Choose at least one original Sony video.')
    requests = []
    for index, source in enumerate(sources):
        output = first_output if index == 0 and first_output else suggested_output(source, level)
        xml = first_xml if index == 0 and first_xml else matching_xml(source)
        xml = str(xml) if xml else None
        requests.append(validate_request(source, output, level, xml))
    input_paths = [request[0] for request in requests]
    output_paths = [request[1] for request in requests]
    if len(set(input_paths)) != len(input_paths):
        raise ValueError('The same original video was selected more than once.')
    if len(set(output_paths)) != len(output_paths):
        raise ValueError('Two videos would be saved to the same output filename.')
    if set(input_paths) & set(output_paths):
        raise ValueError('An output filename matches another selected original video.')
    return requests


def ffmpeg_environment():
    env = os.environ.copy()
    paths = []
    if getattr(sys, 'frozen', False):
        paths.extend((str(Path(sys._MEIPASS)), str(Path(sys.executable).parent)))
    if sys.platform == 'darwin':
        paths.extend(('/opt/homebrew/bin', '/usr/local/bin'))
    env['PATH'] = os.pathsep.join(paths + [env.get('PATH', '')])
    env['PYTHONUNBUFFERED'] = '1'
    return env


def backend_command(source, output, level, xml=None):
    launcher = [sys.executable]
    if not getattr(sys, 'frozen', False):
        launcher += ['-u', str(ROOT / 'app.py')]
    command = launcher + ['--worker', str(source), '--refine', '--level', level, '-o', str(output)]
    if xml is not None:
        command += ['--xml', str(xml)]
    return command


def worker_main(argv):
    # NumPy and OpenCV load in the worker, keeping the window responsive.
    try:
        from stabilize import main as stabilize_main
        stabilize_main(argv)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr, flush=True)
        return 1
    return 0


class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = 'idle'
        self.stage = ''
        self.current = 0
        self.total = 0
        self.output = ''
        self.error = ''
        self.log = deque(maxlen=80)
        self.requests = []
        self.file_index = 0
        self.done_count = 0
        self.failures = []

    def snapshot(self):
        with self.lock:
            return {'state': self.state, 'stage': self.stage, 'current': self.current,
                    'total': self.total, 'output': self.output, 'error': self.error,
                    'log': list(self.log), 'file_index': self.file_index,
                    'file_total': len(self.requests), 'done_count': self.done_count,
                    'source': str(self.requests[self.file_index][0]) if self.requests else '',
                    'failures': list(self.failures)}

    def start(self, source, output, level, xml=None):
        self.start_many([(source, output, xml)], level)

    def start_many(self, requests, level):
        validated = [validate_request(str(source), str(output), level,
                                      str(xml) if xml else None)
                     for source, output, xml in requests]
        if not validated:
            raise ValueError('Choose at least one original Sony video.')
        if len({item[1] for item in validated}) != len(validated):
            raise ValueError('Two videos would be saved to the same output filename.')
        if {item[0] for item in validated} & {item[1] for item in validated}:
            raise ValueError('An output filename matches another selected original video.')
        env = ffmpeg_environment()
        if not shutil.which('ffmpeg', path=env['PATH']) or not shutil.which('ffprobe', path=env['PATH']):
            raise ValueError('FFmpeg and ffprobe are missing. Install the desktop package again or add FFmpeg to PATH.')
        with self.lock:
            if self.state == 'running':
                raise ValueError('A video is already being stabilized.')
            self.state = 'running'
            self.stage = 'Starting stabilization'
            self.current = self.total = 0
            self.requests = validated
            self.file_index = 0
            self.done_count = 0
            self.failures = []
            self.output = str(validated[0][1])
            self.error = ''
            self.log.clear()
        try:
            process = self._launch(validated[0], level, env)
        except OSError as exc:
            with self.lock:
                self.state = 'failed'
                self.error = str(exc)
            raise ValueError(f'Could not start stabilization: {exc}') from exc
        threading.Thread(target=self._monitor, args=(process, level, env), daemon=True).start()

    def _launch(self, request, level, env):
        source, output, xml = request
        return subprocess.Popen(backend_command(source, output, level, xml),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace',
                                       bufsize=1, cwd=ROOT if not getattr(sys, 'frozen', False) else None,
                                       env=env)

    def _monitor(self, process, level, env):
        for index, request in enumerate(self.requests):
            if index:
                with self.lock:
                    self.file_index = index
                    self.output = str(request[1])
                    self.current = self.total = 0
                    self.stage = 'Starting stabilization'
                    self.log.append(f'Processing {request[0].name}')
                try:
                    process = self._launch(request, level, env)
                except OSError as exc:
                    with self.lock:
                        self.failures.append((str(request[0]), str(exc)))
                        self.log.append(f'Failed: {request[0].name}: {exc}')
                    continue
            self._monitor_one(process)
        with self.lock:
            if self.failures:
                self.state = 'failed'
                self.stage = 'Finished with errors' if len(self.requests) > 1 else 'Could not finish'
                failed_names = ', '.join(Path(source).name for source, _ in self.failures[:3])
                if len(self.failures) > 3:
                    failed_names += ', …'
                self.error = (self.failures[0][1] if len(self.requests) == 1 else
                              f'{self.done_count} of {len(self.requests)} videos saved; '
                              f'{len(self.failures)} failed: {failed_names}. See processing details.')
            else:
                self.state = 'done'
                self.stage = 'Videos saved' if len(self.requests) > 1 else 'Video saved'

    def _monitor_one(self, process):
        last_error = ''
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            match = PROGRESS.match(line)
            if line.startswith('Error:'):
                last_error = line.removeprefix('Error: ').strip()
            with self.lock:
                self.log.append(line)
                if match:
                    self.stage = STAGES[match.group(1)]
                    self.current = int(match.group(2))
                    self.total = int(match.group(3))
                elif line.startswith('Checking gyro sync'):
                    self.stage = 'Checking gyro timing'
                elif line.startswith('Removing small residual'):
                    self.stage = 'Refining stabilization'
                elif line.startswith('Gyro:'):
                    self.stage = 'Planning camera motion'
        process.stdout.close()
        code = process.wait()
        with self.lock:
            if code == 0 and Path(self.output).is_file():
                self.done_count += 1
                self.current = self.total or 1
                self.total = self.total or 1
            else:
                error = last_error or f'Stabilization stopped with exit code {code}. See processing details.'
                self.failures.append((str(self.requests[self.file_index][0]), error))
                self.log.append(f'Failed: {self.requests[self.file_index][0].name}: {error}')


class DesktopApp:
    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.root = tk.Tk()
        self.root.title('Sony Video Stabilizer')
        self.root.geometry('780x690')
        self.root.minsize(620, 640)
        self.root.configure(bg='#eef3f2')
        self.root.protocol('WM_DELETE_WINDOW', self._close)
        self.job = JobState()
        self.source = tk.StringVar()
        self.sources = ()
        self.xml = tk.StringVar()
        self.output = tk.StringVar()
        self.level = tk.StringVar(value='balanced')
        self.auto_output = ''
        self.auto_xml = ''
        self.last_source = ''
        self.last_log = ()
        self._build()
        self.root.after(150, self._refresh)

    def _build(self):
        tk, ttk = self.tk, self.ttk
        style = ttk.Style(self.root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('App.TFrame', background='#eef3f2')
        style.configure('App.TLabel', background='#eef3f2', foreground='#27414a', font=('Arial', 11))
        style.configure('Title.TLabel', background='#18323d', foreground='#ffffff', font=('Arial', 21, 'bold'))
        style.configure('Subtitle.TLabel', background='#18323d', foreground='#bad0d1', font=('Arial', 11))
        style.configure('Section.TLabel', background='#eef3f2', foreground='#18323d', font=('Arial', 13, 'bold'))
        style.configure('Hint.TLabel', background='#eef3f2', foreground='#587078', font=('Arial', 10))
        style.configure('App.TRadiobutton', background='#eef3f2', foreground='#27414a', font=('Arial', 11))
        style.configure('Accent.TButton', padding=(18, 10), font=('Arial', 11, 'bold'))
        style.configure('App.Horizontal.TProgressbar', troughcolor='#d3dfdd', background='#197965')

        header = tk.Frame(self.root, bg='#18323d', padx=30, pady=19)
        header.pack(fill='x')
        ttk.Label(header, text='Sony Video Stabilizer', style='Title.TLabel').pack(anchor='w')
        ttk.Label(header, text='Stabilize the camera original using its recorded motion data.',
                  style='Subtitle.TLabel').pack(anchor='w', pady=(6, 0))

        content = ttk.Frame(self.root, style='App.TFrame', padding=(30, 18))
        content.pack(fill='both', expand=True)
        content.columnconfigure(0, weight=1)

        ttk.Label(content, text='Original video', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        self.source_hint = ttk.Label(content, text='Choose one or more original MP4 or MOV videos.',
                                     style='Hint.TLabel', wraplength=690)
        self.source_hint.grid(row=1, column=0, sticky='w', pady=(3, 8))
        self.source_entry = self._path_row(content, 2, self.source, self._browse_source)
        self.source_entry.bind('<FocusOut>', lambda _event: self._source_changed())

        ttk.Label(content, text='Sony XML sidecar', style='Section.TLabel').grid(
            row=3, column=0, sticky='w', pady=(16, 0))
        ttk.Label(content, text='Auto-filled for the first video; each additional video uses its matching XML.',
                  style='Hint.TLabel').grid(row=4, column=0, sticky='w', pady=(3, 8))
        self.xml_entry = self._path_row(content, 5, self.xml, self._browse_xml)

        ttk.Separator(content).grid(row=6, column=0, sticky='ew', pady=15)
        ttk.Label(content, text='Stabilization strength', style='Section.TLabel').grid(row=7, column=0, sticky='w')
        ttk.Label(content, text='Stronger settings smooth more motion and can crop more of the frame.',
                  style='Hint.TLabel').grid(row=8, column=0, sticky='w', pady=(3, 9))
        choices = ttk.Frame(content, style='App.TFrame')
        choices.grid(row=9, column=0, sticky='w')
        self.level_buttons = []
        for index, (name, crop) in enumerate((('Light', 'up to 6% crop'),
                                               ('Balanced', 'up to 10% crop'),
                                               ('Strong', 'up to 20% crop'))):
            button = ttk.Radiobutton(choices, text=f'{name}  ·  {crop}', variable=self.level,
                                     value=name.lower(), command=self._suggest_output,
                                     style='App.TRadiobutton')
            button.grid(row=index, column=0, sticky='w', pady=3)
            self.level_buttons.append(button)

        ttk.Separator(content).grid(row=10, column=0, sticky='ew', pady=15)
        ttk.Label(content, text='Save stabilized video', style='Section.TLabel').grid(row=11, column=0, sticky='w')
        self.output_hint = ttk.Label(content,
                                     text='Keeps the original resolution, targets its bitrate, and copies its audio.',
                                     style='Hint.TLabel', wraplength=690)
        self.output_hint.grid(row=12, column=0, sticky='w', pady=(3, 8))
        self.output_entry = self._path_row(content, 13, self.output, self._browse_output)

        bottom = ttk.Frame(content, style='App.TFrame')
        bottom.grid(row=14, column=0, sticky='ew', pady=(16, 0))
        self.start_button = ttk.Button(bottom, text='Stabilize video', command=self._start,
                                       style='Accent.TButton')
        self.start_button.pack(side='left')
        ttk.Label(bottom, text='Existing files are protected.', style='Hint.TLabel').pack(side='left', padx=15)

        progress = ttk.Frame(content, style='App.TFrame')
        progress.grid(row=15, column=0, sticky='ew', pady=(16, 0))
        progress.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(progress, text='Ready', style='Section.TLabel')
        self.status_label.grid(row=0, column=0, sticky='w')
        self.count_label = ttk.Label(progress, text='', style='Hint.TLabel')
        self.count_label.grid(row=0, column=1, sticky='e')
        self.bar = ttk.Progressbar(progress, maximum=100, mode='determinate',
                                   style='App.Horizontal.TProgressbar')
        self.bar.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(9, 0))
        self.detail_label = ttk.Label(progress, text='Choose a video to begin.', style='Hint.TLabel',
                                       wraplength=690)
        self.detail_label.grid(row=2, column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.log_text = tk.Text(progress, height=4, bg='#e2ebea', fg='#36565c',
                                relief='flat', font=('Menlo' if sys.platform == 'darwin' else 'Consolas', 9),
                                wrap='word', state='disabled')
        self.log_text.grid(row=3, column=0, columnspan=2, sticky='nsew', pady=(12, 0))

    def _path_row(self, parent, row, variable, browse):
        frame = self.ttk.Frame(parent, style='App.TFrame')
        frame.grid(row=row, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        entry = self.ttk.Entry(frame, textvariable=variable)
        entry.grid(row=0, column=0, sticky='ew', ipady=5)
        button = self.ttk.Button(frame, text='Browse…', command=browse)
        button.grid(row=0, column=1, padx=(8, 0))
        if not hasattr(self, 'browse_buttons'):
            self.browse_buttons = []
        self.browse_buttons.append(button)
        return entry

    def _browse_source(self):
        paths = self.filedialog.askopenfilenames(title='Choose original Sony videos',
                                                 filetypes=[('Video files', '*.mp4 *.mov'), ('All files', '*.*')])
        if paths:
            self.sources = tuple(paths)
            self.source.set(paths[0])
            self._source_changed()

    def _browse_xml(self):
        initial_dir = Path(self.source.get()).parent if self.source.get() else Path.home()
        path = self.filedialog.askopenfilename(title='Choose Sony XML sidecar',
                                                initialdir=str(initial_dir),
                                                filetypes=[('XML files', '*.xml'), ('All files', '*.*')])
        if path:
            self.xml.set(path)

    def _source_changed(self):
        source = self.source.get()
        if not self.sources or source != self.sources[0]:
            self.sources = (source,) if source else ()
        if source != self.last_source:
            if not self.xml.get() or self.xml.get() == self.auto_xml:
                match = matching_xml(source) if source else None
                self.auto_xml = str(match) if match else ''
                self.xml.set(self.auto_xml)
            self.last_source = source
        count = len(self.sources)
        if count > 1:
            names = ', '.join(Path(path).name for path in self.sources[:3])
            if count > 3:
                names += ', …'
            self.source_hint.configure(text=f'{count} videos selected: {names}')
            self.output_hint.configure(text='The output below is for the first video. Other videos save beside their originals.')
            self.start_button.configure(text=f'Stabilize {count} videos')
        else:
            self.source_hint.configure(text='Choose one or more original MP4 or MOV videos.')
            self.output_hint.configure(text='Keeps the original resolution, targets its bitrate, and copies its audio.')
            self.start_button.configure(text='Stabilize video')
        self._suggest_output()

    def _browse_output(self):
        current = self.output.get() or (suggested_output(self.source.get(), self.level.get())
                                         if self.source.get() else 'stabilized.mov')
        path = self.filedialog.asksaveasfilename(title='Save stabilized video',
                                                  initialdir=str(Path(current).parent),
                                                  initialfile=Path(current).name,
                                                  defaultextension='.mov',
                                                  filetypes=[('MOV video', '*.mov'), ('MP4 video', '*.mp4')])
        if path:
            self.output.set(path)
            self.auto_output = ''

    def _suggest_output(self):
        source = self.source.get()
        if source and (not self.output.get() or self.output.get() == self.auto_output):
            self.auto_output = suggested_output(source, self.level.get())
            self.output.set(self.auto_output)

    def _start(self):
        try:
            self._source_changed()
            requests = batch_requests(self.sources, self.level.get(),
                                      self.output.get(), self.xml.get())
            self.job.start_many(requests, self.level.get())
        except ValueError as exc:
            self.messagebox.showerror('Cannot stabilize video', str(exc), parent=self.root)
            return

    def _refresh(self):
        snap = self.job.snapshot()
        running = snap['state'] == 'running'
        self.start_button.configure(state='disabled' if running else 'normal')
        for widget in (self.source_entry, self.xml_entry, self.output_entry,
                       *self.browse_buttons, *self.level_buttons):
            widget.configure(state='disabled' if running else 'normal')
        self.status_label.configure(text=snap['stage'] or 'Ready')
        if running and snap['file_total'] > 1:
            file_count = f"Video {snap['file_index'] + 1} / {snap['file_total']}"
            frame_count = f" · {snap['current']} / {snap['total']} frames" if snap['total'] else ''
            self.count_label.configure(text=file_count + frame_count)
        else:
            self.count_label.configure(text=(f"{snap['current']} / {snap['total']} frames"
                                             if running and snap['total'] else ''))
        if running and not snap['total']:
            if str(self.bar['mode']) != 'indeterminate':
                self.bar.configure(mode='indeterminate')
                self.bar.start(15)
        else:
            if str(self.bar['mode']) == 'indeterminate':
                self.bar.stop()
                self.bar.configure(mode='determinate')
            self.bar['value'] = (100 * snap['current'] / snap['total'] if snap['total']
                                 else (100 if snap['state'] == 'done' else 0))
        if snap['state'] == 'done':
            detail = (f"Saved {snap['done_count']} videos."
                      if snap['file_total'] > 1 else f"Saved to {snap['output']}")
        elif snap['state'] == 'failed':
            detail = snap['error']
        elif running:
            detail = (f"Processing {Path(snap['source']).name} at full resolution. Keep this window open."
                      if snap['file_total'] > 1 else 'Processing at full resolution. Keep this window open.')
        else:
            detail = 'Choose a video to begin.'
        self.detail_label.configure(text=detail)
        lines = tuple(snap['log'])
        if lines != self.last_log:
            self.log_text.configure(state='normal')
            self.log_text.delete('1.0', 'end')
            self.log_text.insert('end', '\n'.join(lines))
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
            self.last_log = lines
        self.root.after(150, self._refresh)

    def _close(self):
        if self.job.snapshot()['state'] == 'running':
            self.messagebox.showinfo('Video is processing',
                                     'Wait until the video finishes before closing the app.', parent=self.root)
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        raise SystemExit(worker_main(sys.argv[2:]))
    try:
        DesktopApp().run()
    except (ImportError, ModuleNotFoundError) as exc:
        raise SystemExit(f'Tk desktop support is missing from this Python installation: {exc}') from exc


if __name__ == '__main__':
    main()
