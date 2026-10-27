"""拾音：本地录音笔记。所有 Tk 操作均在主线程执行。"""
import logging
import array
import calendar
import math
import os
import queue
import threading
import time
import uuid
import wave
import winsound
from audio_media import audio_path, encode_mp3, PreviewAudio, NativePlayer
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from audio_import import import_audio
from core import (DATA, ROOT, ASR_MODELS, SUMMARY_STYLES, summary_prompt, simplified,
                  atomic_json, load_config, save_config, read_index, api, summarize, timestamp,
                  api_fingerprint, load_verification, save_verification, clear_verification, overwrite_text)
from core import delete_record

logging.basicConfig(filename=DATA / 'app.log', level=logging.INFO, encoding='utf-8',
                    format='%(asctime)s %(levelname)s %(message)s')

WAVE_POINTS = 640  # 10ms 一点，显示 6.4 秒历史，横向移动速度减半。
WAVE_BARS = 320  # 横向显示密度独立于采样密度和刷新率。
WAVE_SAMPLES = 160  # 16kHz 下每 10ms 提取一组包络。
WAVE_FRAME_MS = 1000 / 30


class WaveHistory(deque):
    """保留绝对采样序号，让历史竖线的分组边界不随窗口移动。"""
    def __init__(self):
        self.lock = threading.Lock()
        super().__init__([0] * WAVE_POINTS, maxlen=WAVE_POINTS)
        self.total = 0
        self.updated = time.monotonic()

    def append(self, value):
        self.extend([value])

    def extend(self, values):
        values = list(values)
        with self.lock:
            super().extend(values)
            self.total += len(values)
            self.updated = time.monotonic()

    def reset(self, total=0):
        with self.lock:
            super().clear()
            super().extend([0] * WAVE_POINTS)
            self.total = total
            self.updated = time.monotonic()

    def snapshot(self):
        with self.lock:
            return list(self), self.total, self.updated


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('拾音 · 本地录音笔记')
        icon = Path(__file__).resolve().parent / 'assets' / 'shiyin.ico'
        if icon.exists():
            self.iconbitmap(str(icon))
        self.geometry('1120x760')
        self.minsize(940, 650)
        self.configure(bg='#f4f5f7')
        self.config_data = load_config()
        self.index = read_index()
        self.events = queue.Queue()
        self.recording = False
        self.paused = False
        self.busy = False
        self.verified = bool(load_verification(self.config_data))
        self.verifying = False
        self.processing_id = None
        self.processing_kind = None
        self.editor_files = {}
        self.editor_feedback = {}
        self.save_jobs = {}
        self.dirty_editors = set()
        self.samples = 0
        self.peaks = WaveHistory()
        self.selected = None
        self.model = None
        self.model_name = None
        self.playback_file = None
        self.player = None
        self.seeking = False
        self.playback_peaks = WaveHistory()
        self.next_wave_frame = 0.0
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#f4f5f7')
        style.configure('TLabel', background='#f4f5f7', foreground='#243149')
        style.configure('TButton', padding=(14, 9))
        style.configure('Accent.TButton', background='#3869db', foreground='white')
        self.configure_checkmark(style)
        header = ttk.Frame(self, padding=(28, 22))
        header.pack(fill='x')
        ttk.Label(header, text='拾音', font=('Microsoft YaHei UI', 24, 'bold')).pack(side='left')
        ttk.Label(header, text='  每一次倾听，都值得留下。', foreground='#7c8596').pack(side='left', padx=15)
        self.task_state = tk.StringVar(value='就绪')
        ttk.Label(header, textvariable=self.task_state, foreground='#3869db').pack(side='right')
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill='both', expand=True, padx=24, pady=(0, 14))
        self.build_recorder()
        self.build_library()
        self.build_settings()
        self.previous_tab = self.tabs.select()
        self.tabs.bind('<<NotebookTabChanged>>', self.tab_changed)
        self.status = tk.StringVar(value='就绪 · 音频与原文保存在本机；开启 AI 纪要后仅将原文发送给所填 API。')
        ttk.Label(self, textvariable=self.status, padding=(26, 12)).pack(fill='x')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.tick)
        self.after(round(WAVE_FRAME_MS), self.animate_waveforms)
        self.refresh_library()
        self.refresh_api_state()

    def build_recorder(self):
        page = ttk.Frame(self.tabs, padding=30)
        self.tabs.add(page, text='  录制  ')
        ttk.Label(page, text='记录此刻', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(page, text='会议、课堂或灵感，用声音开始。', foreground='#7c8596').pack(anchor='w', pady=(6, 20))
        dates = ttk.Frame(page)
        dates.pack()
        self.record_date = tk.StringVar(value=datetime.now().strftime('%Y-%m-%d'))
        ttk.Label(dates, text='录音归属日期').pack(side='left', padx=(0, 10))
        ttk.Entry(dates, textvariable=self.record_date, width=14, state='readonly').pack(side='left')
        ttk.Button(dates, text='选择日期', command=lambda: self.show_calendar(self.record_date, title='选择本次录音日期')).pack(side='left', padx=8)
        ttk.Button(dates, text='今天', command=lambda: self.record_date.set(datetime.now().strftime('%Y-%m-%d'))).pack(side='left')
        self.timer = ttk.Label(page, text='00:00:00', font=('Consolas', 46))
        self.timer.pack(pady=(25, 12))
        self.canvas = tk.Canvas(page, height=140, bg='#ffffff', highlightthickness=0)
        self.canvas.pack(fill='x', pady=12)
        row = ttk.Frame(page)
        row.pack(pady=22)
        self.start_button = ttk.Button(row, text='●  开始录音', style='Accent.TButton', command=self.start_recording)
        self.start_button.pack(side='left', padx=8)
        self.import_button = ttk.Button(row, text='导入录音', command=self.import_recording)
        self.import_button.pack(side='left', padx=8)
        self.pause_button = ttk.Button(row, text='暂停', command=self.pause, state='disabled')
        self.pause_button.pack(side='left', padx=8)
        self.stop_button = ttk.Button(row, text='■  停止并保存', command=self.stop_recording, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        self.ai_enabled = tk.BooleanVar(value=False)
        self.ai_check = ttk.Checkbutton(page, text='本次录音 / 导入生成 AI 纪要', variable=self.ai_enabled, state='disabled', style='Tick.TCheckbutton')
        self.ai_check.pack(pady=10)
        ttk.Label(page, text='支持导入 WAV、MP3、M4A、AAC、FLAC、OGG 等录音。验证 API 后可开启纪要。', foreground='#7c8596').pack()
        self.progress = ttk.Progressbar(page, mode='determinate', maximum=100)
        self.progress.pack(fill='x', pady=(24, 0))

    def configure_checkmark(self, style):
        self.check_images = []
        for selected, disabled in ((False, False), (True, False), (False, True), (True, True)):
            image = tk.PhotoImage(master=self, width=20, height=20)
            border = '#bbc1cc' if disabled else '#68778f'
            fill = ('#aab6cc' if disabled else '#3869db') if selected else '#ffffff'
            image.put(border, to=(1, 1, 19, 19))
            image.put(fill, to=(2, 2, 18, 18))
            if selected:
                for (x1, y1), (x2, y2) in (((4, 10), (8, 14)), ((8, 14), (15, 5))):
                    steps = max(abs(x2-x1), abs(y2-y1))
                    for i in range(steps + 1):
                        x = round(x1 + (x2-x1)*i/steps)
                        y = round(y1 + (y2-y1)*i/steps)
                        image.put('#ffffff', to=(x, y, x+2, y+2))
            self.check_images.append(image)
        off, on, off_disabled, on_disabled = self.check_images
        style.element_create('TickCheck.indicator', 'image', off,
                             ('selected', 'disabled', on_disabled), ('selected', on), ('disabled', off_disabled))
        style.layout('Tick.TCheckbutton', [('Checkbutton.padding', {'sticky': 'nswe', 'children': [
            ('TickCheck.indicator', {'side': 'left'}),
            ('Checkbutton.label', {'side': 'left', 'sticky': 'w'})]})])

    def build_library(self):
        page = ttk.Frame(self.tabs, padding=22)
        self.tabs.add(page, text='  笔记库  ')
        toolbar = ttk.Frame(page)
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='录音日期').pack(side='left')
        self.date = tk.StringVar(value=datetime.now().strftime('%Y-%m-%d'))
        ttk.Entry(toolbar, textvariable=self.date, width=14).pack(side='left', padx=10)
        ttk.Button(toolbar, text='查找', command=self.refresh_library).pack(side='left')
        ttk.Button(toolbar, text='日历选日期', command=self.show_calendar).pack(side='left', padx=8)
        ttk.Button(toolbar, text='显示全部', command=lambda: self.refresh_library(True)).pack(side='right')
        selector = ttk.Frame(page)
        selector.pack(fill='x', pady=(12, 2))
        ttk.Label(selector, text='选择录音').pack(side='left')
        self.records = ttk.Combobox(selector, state='readonly')
        self.records.pack(side='left', fill='x', expand=True, padx=(10, 0))
        self.records.bind('<<ComboboxSelected>>', self.select_record)
        right = ttk.Frame(page)
        right.pack(fill='both', expand=True)
        self.info = tk.StringVar(value='选择一条录音以查看')
        ttk.Label(right, textvariable=self.info, wraplength=950).pack(anchor='w', pady=6)
        actions = ttk.Frame(right)
        actions.pack(fill='x', pady=4)
        ttk.Button(actions, text='▶ 播放', command=self.play).pack(side='left')
        ttk.Button(actions, text='停止播放', command=lambda: self.stop_playback(notify=True)).pack(side='left', padx=6)
        ttk.Button(actions, text='打开文件夹', command=self.open_folder).pack(side='left')
        self.retranscribe_button = ttk.Button(actions, text='重新转写', command=self.retranscribe)
        self.retranscribe_button.pack(side='left', padx=6)
        self.retry_button = ttk.Button(actions, text='重新生成纪要', command=self.retry)
        self.retry_button.pack(side='left', padx=6)
        ttk.Button(actions, text='修改日期', command=self.edit_record_date).pack(side='left')
        self.delete_button = ttk.Button(actions, text='删除录音', command=self.delete_selected)
        self.delete_button.pack(side='left', padx=6)
        self.library_status = tk.StringVar(value='就绪 · 选择录音后可播放、重新处理或编辑文本。')
        ttk.Label(right, textvariable=self.library_status, wraplength=950, foreground='#3869db').pack(fill='x', pady=4)
        self.playback_canvas = tk.Canvas(right, height=80, bg='white', highlightthickness=0)
        self.playback_canvas.pack(fill='x', pady=6)
        self.playback_canvas.bind('<Configure>', lambda event: self.draw_peaks(self.playback_canvas, self.playback_peaks))
        self.playback_time = ttk.Label(right, text='00:00:00 / 00:00:00')
        self.playback_time.pack(anchor='w')
        self.playback_position = tk.DoubleVar(value=0)
        self.seek_bar = ttk.Scale(right, from_=0, to=1, variable=self.playback_position,
                                  command=self.preview_seek)
        self.seek_bar.pack(fill='x', pady=(0, 6))
        self.seek_bar.bind('<ButtonPress-1>', self.begin_seek)
        self.seek_bar.bind('<ButtonRelease-1>', self.end_seek)
        self.previews = ttk.Panedwindow(right, orient='horizontal')
        self.previews.pack(fill='both', expand=True)
        self.summary = self.text_view(self.previews, 'AI 纪要', weight=1, height=9)
        self.transcript = self.text_view(self.previews, '录音原文', weight=3, height=9)
        self.preview_sized = False
        self.previews.bind('<Configure>', self.size_previews)

    def size_previews(self, event=None):
        if not self.preview_sized and self.previews.winfo_width() > 100:
            self.previews.sashpos(0, self.previews.winfo_width() // 4)
            self.preview_sized = True

    def text_view(self, notebook, title, weight=1, height=5):
        frame = ttk.Labelframe(notebook, text=title)
        notebook.add(frame, weight=weight)
        text = tk.Text(frame, height=height, wrap='word', font=('Microsoft YaHei UI', 11), padx=18, pady=10,
                       bg='white', relief='flat', spacing1=4, spacing3=6, state='disabled', undo=True)
        saved = tk.StringVar(value='选择录音后可编辑，修改自动覆盖保存。')
        self.editor_feedback[text] = saved
        ttk.Label(frame, textvariable=saved, foreground='#7c8596').pack(side='bottom', fill='x')
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.pack(side='right', fill='y')
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill='both', expand=True)
        text.bind('<<Modified>>', lambda event, widget=text: self.editor_changed(widget))
        text.bind('<FocusOut>', lambda event: self.flush_edits())
        return text

    def editor_changed(self, widget):
        if not widget.edit_modified():
            return
        widget.edit_modified(False)
        if not self.editor_files.get(widget) or str(widget['state']) == 'disabled':
            return
        self.dirty_editors.add(widget)
        self.editor_feedback[widget].set('正在自动保存…')
        if widget in self.save_jobs:
            self.after_cancel(self.save_jobs.pop(widget))
        self.save_jobs[widget] = self.after(250, lambda: self.save_editor(widget))

    def save_editor(self, widget):
        job = self.save_jobs.pop(widget, None)
        if job:
            self.after_cancel(job)
        if widget not in self.dirty_editors:
            return True
        try:
            path = self.editor_files[widget]
            overwrite_text(path, widget.get('1.0', 'end-1c'))
            self.dirty_editors.discard(widget)
            self.editor_feedback[widget].set('✓ 已自动保存 · ' + datetime.now().strftime('%H:%M:%S'))
            return True
        except OSError as exc:
            self.editor_feedback[widget].set('保存失败，修改仍在编辑框中')
            self.library_status.set('文本保存失败：' + str(exc))
            self.status.set('文本尚未保存，请修复路径或权限后再切换录音。')
            return False

    def flush_edits(self):
        # 包括本轮事件尚未触发 <<Modified>> 的最后一次键入。
        for widget in (self.summary, self.transcript):
            self.editor_changed(widget)
        results = [self.save_editor(widget) for widget in list(self.dirty_editors)]
        return all(results)

    def set_editor_permissions(self):
        locked = self.selected is not None and self.selected['id'] == self.processing_id
        for widget in (self.summary, self.transcript):
            path = self.editor_files.get(widget)
            editable = path is not None and path.parent.is_dir() and not locked
            widget.configure(state='normal' if editable else 'disabled')
            if locked:
                self.editor_feedback[widget].set('正在重新处理，完成后可编辑。')

    def feedback(self, message):
        self.library_status.set(message)
        self.status.set(message)
        self.refresh_task_state()

    def refresh_task_state(self):
        if self.recording:
            state = '录音已暂停' if self.paused else '录制中'
        elif self.busy:
            state = '处理中'
        else:
            state = '就绪'
        self.task_state.set(state)

    def build_settings(self):
        page = ttk.Frame(self.tabs, padding=26)
        self.settings_page = page
        self.tabs.add(page, text='  设置  ')
        page.columnconfigure(1, weight=1)
        self.fields = {}
        labels = [('storage', '默认存储路径'), ('asr_model', '本地转写模型'), ('base_url', 'API Base URL'),
                  ('key', 'API Key'), ('model', '纪要模型')]
        for i, (key, label) in enumerate(labels):
            ttk.Label(page, text=label).grid(row=i, column=0, sticky='w', pady=8, padx=(0, 18))
            var = tk.StringVar(value=self.config_data[key])
            self.fields[key] = var
            if key == 'asr_model':
                entry = ttk.Combobox(page, textvariable=var, values=ASR_MODELS, state='readonly')
            elif key == 'model':
                entry = ttk.Combobox(page, textvariable=var)
                self.model_combo = entry
            else:
                entry = ttk.Entry(page, textvariable=var, show='•' if key == 'key' else '')
            entry.grid(row=i, column=1, sticky='ew', pady=8)
            if key in ('base_url', 'key', 'model'):
                var.trace_add('write', lambda *args: self.invalidate_api())
        ttk.Button(page, text='选择文件夹', command=self.choose_storage).grid(row=0, column=2, padx=10)
        self.fetch_button = ttk.Button(page, text='获取模型', command=self.fetch_models)
        self.fetch_button.grid(row=4, column=2, padx=10)
        ttk.Label(page, text='使用内置 faster-whisper 转写。small 为默认；更大模型更慢、占用更多内存。首次切换模型需联网下载。',
                  wraplength=850, foreground='#7c8596').grid(row=5, column=0, columnspan=3, sticky='w', pady=12)
        for key in ('summary_style', 'meeting_prompt', 'custom_prompt'):
            self.fields[key] = tk.StringVar(value=self.config_data[key])
        self.style_label = tk.StringVar()
        ttk.Label(page, text='AI 纪要风格').grid(row=6, column=0, sticky='w', pady=8)
        self.style_combo = ttk.Combobox(page, textvariable=self.style_label, values=list(SUMMARY_STYLES), state='readonly')
        self.style_combo.grid(row=6, column=1, sticky='ew', pady=8)
        self.style_combo.bind('<<ComboboxSelected>>', self.change_summary_style)
        ttk.Label(page, text='纪要要求（自定义模式可编辑，最多 1000 字）').grid(row=7, column=0, columnspan=3, sticky='w', pady=8)
        self.prompt = tk.Text(page, height=4, font=('Microsoft YaHei UI', 10), wrap='word', relief='flat', padx=10, pady=10)
        self.prompt.insert('1.0', self.config_data['prompt'])
        self.prompt.grid(row=8, column=0, columnspan=3, sticky='ew')
        self.display_summary_style()
        row = ttk.Frame(page)
        row.grid(row=9, column=0, columnspan=3, sticky='w', pady=18)
        ttk.Button(row, text='保存设置', command=self.save_settings, style='Accent.TButton').pack(side='left')
        self.verify_button = ttk.Button(row, text='验证 API 和模型', command=self.verify_api)
        self.verify_button.pack(side='left', padx=12)
        self.api_status = tk.StringVar(value='未验证')
        self.api_feedback = ttk.Label(page, textvariable=self.api_status, wraplength=850)
        self.api_feedback.grid(row=10, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(page, text='API 需兼容 OpenAI /models 与 /chat/completions。密钥使用当前 Windows 用户凭据加密保存。',
                  foreground='#7c8596', wraplength=850).grid(row=11, column=0, columnspan=3, sticky='w')

    def display_summary_style(self):
        style = self.fields['summary_style'].get()
        self.style_label.set(next(label for label, value in SUMMARY_STYLES.items() if value == style))
        self.displayed_style = style
        self.prompt.configure(state='normal')
        self.prompt.delete('1.0', 'end')
        self.prompt.insert('1.0', summary_prompt({key: var.get() for key, var in self.fields.items()}))
        self.prompt.configure(state='normal' if style == 'custom' else 'disabled')

    def change_summary_style(self, event=None):
        if self.displayed_style == 'custom':
            self.fields['custom_prompt'].set(self.prompt.get('1.0', 'end-1c'))
        self.fields['summary_style'].set(SUMMARY_STYLES[self.style_label.get()])
        self.display_summary_style()

    def choose_storage(self):
        value = filedialog.askdirectory(parent=self)
        if value:
            self.fields['storage'].set(value)

    def tab_changed(self, event=None):
        current = self.tabs.select()
        if self.previous_tab == str(self.settings_page) and current != self.previous_tab:
            changed = False
            for key, var in self.fields.items():
                if var.get() != self.config_data[key]:
                    var.set(self.config_data[key])
                    changed = True
            if self.prompt.get('1.0', 'end-1c') != self.config_data['prompt']:
                self.prompt.configure(state='normal')
                self.prompt.delete('1.0', 'end')
                self.prompt.insert('1.0', self.config_data['prompt'])
                changed = True
            self.display_summary_style()
            if changed:
                self.status.set('未保存的设置修改已清除。')
            self.refresh_api_state()
        self.previous_tab = current

    def show_calendar(self, target=None, on_select=None, title='选择录音日期'):
        target = self.date if target is None else target
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self)
        window.resizable(False, False)
        try:
            current = datetime.strptime(target.get(), '%Y-%m-%d')
        except ValueError:
            current = datetime.now()
        self.calendar_month = (current.year, current.month)
        title = tk.StringVar()
        header = ttk.Frame(window, padding=12)
        header.pack(fill='x')
        grid = ttk.Frame(window, padding=12)
        grid.pack()
        dates = {r['date'] for r in self.index}
        def choose(value):
            if on_select is not None:
                if on_select(value) is False:
                    return
            else:
                target.set(value)
                if target is self.date:
                    self.refresh_library()
            window.destroy()
        def render(offset=0):
            year, month = self.calendar_month
            total = year * 12 + month - 1 + offset
            year, month = total // 12, total % 12 + 1
            if not 1 <= year <= 9999:
                return
            self.calendar_month = (year, month)
            title.set(f'{year} 年 {month} 月')
            for child in grid.winfo_children():
                child.destroy()
            for col, label in enumerate('一二三四五六日'):
                ttk.Label(grid, text=label, anchor='center', width=6).grid(row=0, column=col)
            for row, week in enumerate(calendar.monthcalendar(year, month), 1):
                for col, day in enumerate(week):
                    if not day:
                        continue
                    value = f'{year:04}-{month:02}-{day:02}'
                    label = str(day) + (' ●' if value in dates else '')
                    ttk.Button(grid, text=label, width=5,
                               style='Accent.TButton' if value == target.get() else 'TButton',
                               command=lambda date=value: choose(date)).grid(row=row, column=col, padx=2, pady=2)
        ttk.Button(header, text='‹', command=lambda: render(-1)).pack(side='left')
        ttk.Label(header, textvariable=title, anchor='center', width=22).pack(side='left')
        ttk.Button(header, text='›', command=lambda: render(1)).pack(side='left')
        ttk.Label(window, text='● 表示当天有录音', padding=8).pack()
        ttk.Button(window, text='今天', command=lambda: choose(datetime.now().strftime('%Y-%m-%d'))).pack(pady=(0, 12))
        render()
        window.grab_set()

    def edit_record_date(self):
        if not self.selected:
            self.feedback('请先选择需要修改日期的录音。')
            return
        record_id = self.selected['id']
        initial = tk.StringVar(value=self.selected['date'])
        self.show_calendar(initial, lambda value: self.change_record_date(record_id, value), '修改录音归属日期')

    def change_record_date(self, record_id, value):
        if not self.flush_edits():
            return False
        try:
            datetime.strptime(value, '%Y-%m-%d')
            updated = [dict(record, date=value) if record['id'] == record_id else dict(record) for record in self.index]
            if not any(record['id'] == record_id for record in updated):
                raise ValueError('该录音已不存在。')
            atomic_json(DATA / 'library.json', updated)
            self.index = updated
            self.date.set(value)
            self.refresh_library()
            for i, record in enumerate(self.visible_records):
                if record['id'] == record_id:
                    self.records.current(i)
                    self.select_record()
                    break
            self.feedback('录音归属日期已更新为 ' + value)
            return True
        except Exception as exc:
            messagebox.showerror('日期未修改', str(exc), parent=self)
            return False

    def invalidate_api(self):
        self.verified = False
        self.ai_enabled.set(False)
        self.ai_check.configure(state='disabled')
        if hasattr(self, 'api_status'):
            self.api_status.set('API 配置已修改，请保存后验证。')
            self.api_feedback.configure(foreground='#a45d16')

    def refresh_api_state(self):
        draft = {key: self.fields[key].get() for key in ('base_url', 'key', 'model')}
        if api_fingerprint(draft) != api_fingerprint(self.config_data):
            self.invalidate_api()
            return
        if self.verifying:
            self.api_status.set('正在验证实际模型输出，请稍候…')
            self.api_feedback.configure(foreground='#3869db')
            return
        cached = load_verification(self.config_data)
        self.verified = bool(cached)
        self.ai_check.configure(state='normal' if self.verified and not self.recording else 'disabled')
        if cached:
            self.api_status.set(f"✓ 已验证 · {self.config_data['model']} · 上次成功：{cached['verified_at'].replace('T', ' ')}")
            self.api_feedback.configure(foreground='#22834d')
            self.verify_button.configure(text='重新验证 API 和模型')
        else:
            self.ai_enabled.set(False)
            self.api_status.set('尚未验证当前 API 和模型，请点击验证。')
            self.api_feedback.configure(foreground='#a45d16')
            self.verify_button.configure(text='验证 API 和模型')

    def settings_snapshot(self):
        config = {key: var.get().strip() for key, var in self.fields.items()}
        if config['summary_style'] == 'custom':
            config['custom_prompt'] = self.prompt.get('1.0', 'end').strip()
        config['prompt'] = summary_prompt(config)
        if len(config['custom_prompt']) > 1000:
            raise ValueError('纪要要求不能超过 1000 字。')
        if not config['storage'] or not config['asr_model']:
            raise ValueError('存储路径和转写模型不能为空。')
        if config['asr_model'] not in ASR_MODELS:
            raise ValueError('请从列表选择内置转写模型。')
        return config

    def save_settings(self):
        try:
            config = self.settings_snapshot()
            Path(config['storage']).mkdir(parents=True, exist_ok=True)
            save_config(config)
            self.config_data = config
            self.fields['custom_prompt'].set(config['custom_prompt'])
            self.refresh_api_state()
            self.status.set('设置已保存')
            return config
        except Exception as exc:
            messagebox.showerror('设置未保存', str(exc), parent=self)

    def background(self, work):
        def run():
            try:
                work()
            except Exception as exc:
                logging.exception('后台任务失败')
                self.events.put(('error', str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def fetch_models(self):
        try:
            config = self.settings_snapshot()
            self.status.set('正在获取模型列表…')
            self.fetch_button.configure(state='disabled', text='正在获取…')
            def work():
                try:
                    models = [x['id'] for x in api(config, '/models').get('data', [])]
                    self.events.put(('models', models))
                except Exception as exc:
                    self.events.put(('models_error', str(exc)))
            self.background(work)
        except ValueError as exc:
            messagebox.showerror('设置有误', str(exc), parent=self)

    def verify_api(self):
        if self.verifying:
            return
        try:
            config = self.settings_snapshot()
            if config != self.config_data:
                raise ValueError('请先保存设置，再验证 API 和模型。')
            self.invalidate_api()
            self.verifying = True
            self.verify_button.configure(state='disabled', text='正在验证…')
            self.refresh_api_state()
            self.status.set('正在验证模型实际输出…')
            def work():
                try:
                    summarize(config, '', True)
                    self.events.put(('api_validation', (config, None)))
                except Exception as exc:
                    logging.exception('API 验证失败')
                    self.events.put(('api_validation', (config, str(exc))))
            self.background(work)
        except ValueError as exc:
            messagebox.showerror('设置未保存', str(exc), parent=self)

    def import_recording(self):
        if self.busy or self.recording or not self.flush_edits():
            return
        source = filedialog.askopenfilename(parent=self, title='选择要导入的录音', filetypes=[
            ('音频文件', '*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wma *.aiff *.aif *.amr *.mp4 *.webm'),
            ('所有文件', '*.*')])
        if not source:
            return
        config = dict(self.config_data)
        date = self.record_date.get()
        use_ai = self.ai_enabled.get() and self.verified
        self.stop_playback()
        self.busy = True
        self.processing_kind = 'import'
        for button in (self.start_button, self.import_button, self.retry_button, self.retranscribe_button):
            button.configure(state='disabled')
        self.ai_check.configure(state='disabled')
        self.progress.configure(mode='indeterminate')
        self.progress.start()
        self.feedback('正在导入录音：' + Path(source).name)

        def work():
            try:
                record = import_audio(source, config['storage'], date, use_ai,
                                      lambda message: self.events.put(('status', message)))
                self.events.put(('imported', (record, use_ai, config)))
            except Exception as exc:
                logging.exception('导入录音失败')
                self.events.put(('import_error', str(exc)))
        self.background(work)

    def finish_import(self, record, use_ai, config):
        self.busy = False
        self.processing_kind = None
        self.progress.stop()
        self.progress.configure(mode='determinate', value=0)
        for button in (self.start_button, self.import_button, self.retry_button, self.retranscribe_button):
            button.configure(state='normal')
        self.ai_check.configure(state='normal' if self.verified else 'disabled')
        try:
            updated = self.index + [record]
            atomic_json(DATA / 'library.json', updated)
            self.index = updated
        except Exception as exc:
            self.feedback('导入索引保存失败，已转换音频保留在：' + record['folder'])
            messagebox.showerror('导入未完成', f"{exc}\n音频保留在：{record['folder']}", parent=self)
            return
        self.date.set(record['date'])
        self.refresh_library()
        for i, item in enumerate(self.visible_records):
            if item['id'] == record['id']:
                self.records.current(i)
                self.select_record()
                break
        self.process(record, use_ai, config)

    def start_recording(self):
        if self.busy or self.recording:
            return
        try:
            import sounddevice as sd
            self.stop_playback()
            self.pending = DATA / ('pending-' + uuid.uuid4().hex + '.wav')
            self.wav = wave.open(str(self.pending), 'wb')
            self.wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            self.samples = 0
            self.reset_waveform()
            self.timer.configure(text='00:00:00')
            self.progress['value'] = 0
            self.paused = False
            self.capture_ai = self.ai_enabled.get() and self.verified
            self.capture_config = dict(self.config_data)
            self.started_at = datetime.now().isoformat(timespec='seconds')
            self.audio_queue = queue.Queue(maxsize=256)
            self.capture_error = None
            def callback(indata, frames, time_info, status):
                if status:
                    self.capture_error = str(status)
                if not self.paused:
                    raw = bytes(indata)
                    try:
                        self.audio_queue.put_nowait(raw)
                    except queue.Full:
                        self.capture_error = '磁盘写入过慢，录音缓冲溢出。'
            def writer():
                try:
                    while True:
                        raw = self.audio_queue.get()
                        if raw is None:
                            break
                        self.wav.writeframes(raw)
                        self.samples += len(raw) // 2
                        self.peaks.extend(self.audio_envelope(raw))
                except Exception as exc:
                    self.capture_error = str(exc)
            self.writer = threading.Thread(target=writer, daemon=True)
            self.writer.start()
            self.stream = sd.RawInputStream(samplerate=16000, channels=1, dtype='int16', blocksize=WAVE_SAMPLES, callback=callback)
            self.stream.start()
            self.recording = True
            self.import_button.configure(state='disabled')
            self.start_button.configure(state='disabled')
            self.pause_button.configure(state='normal')
            self.stop_button.configure(state='normal')
            self.ai_check.configure(state='disabled')
            self.status.set('正在录音 · 原始音频持续写入磁盘')
            self.refresh_task_state()
        except Exception as exc:
            if hasattr(self, 'audio_queue'):
                self.audio_queue.put(None)
                self.writer.join(timeout=2)
            if hasattr(self, 'wav'):
                self.wav.close()
            messagebox.showerror('无法录音', str(exc), parent=self)

    def pause(self):
        self.paused = not self.paused
        self.pause_button.configure(text='继续录音' if self.paused else '暂停')
        self.status.set('录音已暂停' if self.paused else '正在录音')
        self.refresh_task_state()

    def stop_recording(self):
        if not self.recording:
            return
        self.stream.stop()
        self.stream.close()
        self.audio_queue.put(None)
        self.writer.join()
        self.wav.close()
        self.recording = False
        self.import_button.configure(state='normal')
        self.paused = False
        self.reset_waveform()
        self.pause_button.configure(state='disabled', text='暂停')
        self.stop_button.configure(state='disabled')
        if self.capture_error:
            messagebox.showwarning('录音设备提示', self.capture_error + '\n音频已保留，请检查是否完整。', parent=self)
        storage = Path(self.capture_config['storage'])
        try:
            storage.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.start_button.configure(state='normal')
            self.ai_check.configure(state='normal' if self.verified else 'disabled')
            messagebox.showerror('存储路径不可用', f'{exc}\n录音保留在：{self.pending}', parent=self)
            return
        target = filedialog.asksaveasfilename(parent=self, title='文件保存确认：文件名将作为本次录音文件夹名称',
                    initialdir=str(storage), initialfile='录音_' + self.record_date.get().replace('-', '') + datetime.now().strftime('_%H%M%S'),
                    defaultextension='.mp3', filetypes=[('MP3 录音', '*.mp3')])
        if target:
            folder = Path(target).with_suffix('')
        else:
            self.pending.unlink(missing_ok=True)
            self.start_button.configure(state='normal')
            self.ai_check.configure(state='normal' if self.verified else 'disabled')
            self.status.set('已取消保存，本次录音已丢弃。')
            return
        base = folder
        i = 2
        while folder.exists():
            folder = base.with_name(base.name + f'_{i}')
            i += 1
        try:
            folder.mkdir(parents=True)
            record = dict(id=uuid.uuid4().hex, title=folder.name, date=self.record_date.get(),
                          created=self.started_at, duration=self.samples / 16000, folder=str(folder), state='等待转写')
            self.busy = True
            self.processing_kind = 'import'
            self.start_button.configure(state='disabled')
            self.import_button.configure(state='disabled')
            self.progress.configure(mode='indeterminate')
            self.progress.start()
            pending, use_ai, config = self.pending, self.capture_ai, dict(self.capture_config)
            self.feedback('正在保存 MP3 录音…')
            def work():
                try:
                    encode_mp3(pending, folder / 'audio.mp3', lambda message: self.events.put(('status', message)))
                    (folder / 'transcript.txt').write_text('等待本地转写…', encoding='utf-8')
                    (folder / 'summary.md').write_text('等待生成纪要…' if use_ai else '本次录音未开启 AI 纪要。', encoding='utf-8')
                    pending.unlink()
                    self.events.put(('imported', (record, use_ai, config)))
                except Exception as exc:
                    logging.exception('MP3 保存失败')
                    self.events.put(('import_error', f'{exc}\n恢复录音保留在：{pending}'))
            self.background(work)
        except Exception as exc:
            self.start_button.configure(state='normal')
            messagebox.showerror('保存失败', f'{exc}\n恢复音频保留在：{self.pending}', parent=self)

    def process(self, record, use_ai, config):
        if self.busy:
            self.feedback('已有转写任务正在进行，请等待完成。')
            return
        if not self.flush_edits():
            return
        self.busy = True
        self.processing_id = record['id']
        self.processing_kind = 'transcribe'
        self.import_button.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.retry_button.configure(state='disabled', text='正在重新处理…')
        self.retranscribe_button.configure(state='disabled', text='正在转写…')
        self.set_editor_permissions()
        self.feedback('正在处理：' + record['title'] + (' · 本地转写后生成纪要' if use_ai else ' · 仅重新转写，保留现有纪要'))
        self.progress['value'] = 0
        config = dict(config)
        def work():
            folder = Path(record['folder'])
            state = '转写失败'
            try:
                self.events.put(('status', '加载本地转写模型…首次使用需要下载，请稍候。'))
                from faster_whisper import WhisperModel
                name = config['asr_model']
                if self.model is None or self.model_name != name:
                    from model_store import ensure_model
                    source = ensure_model(name, ROOT / 'models', DATA.parent / 'models',
                                          lambda message: self.events.put(('status', message)))
                    self.model = WhisperModel(str(source), device='cpu', compute_type='int8', local_files_only=True)
                    self.model_name = name
                segments, _ = self.model.transcribe(str(audio_path(folder)), vad_filter=True, beam_size=5)
                lines = []
                for segment in segments:
                    lines.append(f'[{timestamp(segment.start)} → {timestamp(segment.end)}] {simplified(segment.text.strip())}')
                    self.events.put(('progress', min(99, segment.end / max(record['duration'], 1) * 100)))
                    logging.info('转写 %s %.1f / %.1f 秒', record['id'], segment.end, record['duration'])
                transcript = '\n'.join(lines) or '（未识别到语音）'
                (folder / 'transcript.txt').write_text(transcript, encoding='utf-8')
                state = '完成'
                if use_ai and lines:
                    state = '纪要失败'
                    self.events.put(('status', '本地转写完成，正在生成 AI 纪要…'))
                    result = summarize(config, transcript)
                    (folder / 'summary.md').write_text(result, encoding='utf-8')
                    state = '完成'
                elif use_ai:
                    (folder / 'summary.md').write_text('未识别到语音，已跳过 AI 纪要。', encoding='utf-8')
            except Exception as exc:
                logging.exception('处理失败 %s', record['id'])
                self.events.put(('processing_error', (state, str(exc), config)))
            finally:
                self.events.put(('done', (record['id'], state)))
        self.background(work)

    def refresh_library(self, all_dates=False):
        if not self.flush_edits():
            return
        if not all_dates:
            try:
                datetime.strptime(self.date.get(), '%Y-%m-%d')
            except ValueError:
                messagebox.showerror('日期格式', '请填写 YYYY-MM-DD，例如 2026-10-25。', parent=self)
                return
        self.stop_playback()
        self.library_all_dates = all_dates
        self.visible_records = sorted([r for r in self.index if all_dates or r['date'] == self.date.get()], key=lambda r: r['created'])
        self.records['values'] = [f"{r['date']}  {r['created'][11:]} · {r['title']} · {timestamp(r['duration'])}"
                                  for r in self.visible_records]
        self.selected = None
        if self.visible_records:
            self.records.current(0)
            self.select_record()
        else:
            self.records.set('当天没有录音')
            self.info.set('这一天还没有录音。开始录制，留住第一条笔记。')
            self.fill(self.transcript, '')
            self.fill(self.summary, '')
            self.editor_files.clear()
            for label in self.editor_feedback.values():
                label.set('选择录音后可编辑，修改自动覆盖保存。')
        if hasattr(self, 'library_status'):
            self.feedback(f"找到 {len(self.visible_records)} 条录音" + (' · 全部日期' if all_dates else ' · ' + self.date.get()))

    @staticmethod
    def fill(widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.edit_reset()
        widget.edit_modified(False)
        widget.configure(state='disabled')

    def select_record(self, event=None):
        selection = self.records.current()
        if selection < 0 or selection >= len(self.visible_records):
            return
        if not self.flush_edits():
            if self.selected in self.visible_records:
                self.records.current(self.visible_records.index(self.selected))
            return
        self.stop_playback()
        self.selected = self.visible_records[selection]
        r = self.selected
        self.seek_bar.configure(to=max(r['duration'], 0.001))
        self.playback_position.set(0)
        self.playback_time.configure(text=f'00:00:00 / {timestamp(r["duration"])}')
        time_label = '导入时间' if r.get('source') == 'import' else '实际录制'
        self.info.set(f"{r['title']}\n归属日期：{r['date']}  ·  {time_label}：{r['created'].replace('T', '  ')}  ·  {timestamp(r['duration'])}  ·  {r['state']}")
        for widget, filename in [(self.transcript, 'transcript.txt'), (self.summary, 'summary.md')]:
            path = Path(r['folder']) / filename
            self.fill(widget, simplified(path.read_text(encoding='utf-8')) if path.exists() else '文件不存在，可能已被移动。')
            self.editor_files[widget] = path
            self.editor_feedback[widget].set('可直接编辑 · 修改自动覆盖保存')
        self.set_editor_permissions()

    def delete_selected(self):
        if not self.selected:
            self.feedback('请先选择要删除的录音。')
            return
        record = self.selected
        if record['id'] == self.processing_id:
            self.feedback('这条录音正在处理，请等待处理完成后再删除。')
            return
        if not messagebox.askyesno('删除录音？',
                f'确定删除“{record["title"]}”吗？\n将一起永久删除该条录音的音频、原文和纪要，无法撤销。',
                parent=self, icon='warning', default='no'):
            return
        if not self.flush_edits():
            return
        self.stop_playback()
        try:
            self.index, remaining = delete_record(record, self.index, DATA / 'library.json')
        except Exception as exc:
            logging.exception('删除录音失败 %s', record['id'])
            self.feedback('删除失败：' + str(exc))
            messagebox.showerror('删除失败', str(exc), parent=self)
            return
        # 清除自动保存目标，防止刷新或定时保存重新创建已删除文件。
        self.editor_files.clear()
        self.dirty_editors.clear()
        for job in self.save_jobs.values():
            self.after_cancel(job)
        self.save_jobs.clear()
        self.selected = None
        self.refresh_library(getattr(self, 'library_all_dates', False))
        self.feedback('已删除录音及其原文、纪要：' + record['title'])
        if remaining:
            self.feedback('记录已移除，但部分文件清理失败，请查看提示。')
            messagebox.showwarning('部分文件未清理', '记录已从笔记库移除，但以下文件未能删除：\n' + '\n'.join(remaining), parent=self)

    def play(self):
        if self.recording:
            self.feedback('正在录音，请停止录音后再播放。')
            return
        if not self.selected:
            self.feedback('请先选择需要播放的录音。')
            return
        if self.selected:
            try:
                self.stop_playback()
                path = str(audio_path(self.selected['folder']))
                self.playback_file = PreviewAudio(path)
                self.playback_duration = self.playback_file.getnframes() / self.playback_file.getframerate()
                self.seek_bar.configure(to=max(self.playback_duration, 0.001))
                self.player = NativePlayer(path)
                self.player.play()
                self.playback_started = time.monotonic()
                self.feedback('正在播放：' + self.selected['title'])
            except Exception as exc:
                self.stop_playback()
                messagebox.showerror('播放失败', str(exc), parent=self)

    def stop_playback(self, notify=False):
        if self.player is not None:
            try:
                self.player.close()
            except OSError:
                logging.exception('关闭播放器失败')
            self.player = None
        if self.playback_file is not None:
            self.playback_file.close()
            self.playback_file = None
        self.playback_peaks.reset()
        if hasattr(self, 'playback_canvas'):
            self.draw_peaks(self.playback_canvas, self.playback_peaks)
            self.playback_time.configure(text='00:00:00 / 00:00:00')
            if not self.seeking:
                self.playback_position.set(0)
        if notify:
            self.feedback('播放已停止。')

    def update_playback(self):
        if self.playback_file is None:
            return
        elapsed = time.monotonic() - self.playback_started
        if elapsed >= self.playback_duration:
            self.stop_playback()
            self.feedback('播放完成。')
            return
        audio = self.playback_file
        # 只消费已经播放过的完整 10ms 分段，避免重复取样与跳过瞬态。
        rate = audio.getframerate()
        target = min(int(elapsed * rate) // WAVE_SAMPLES * WAVE_SAMPLES, audio.getnframes())
        if target - audio.tell() > WAVE_POINTS * WAVE_SAMPLES:
            start = target - WAVE_POINTS * WAVE_SAMPLES
            audio.setpos(start)
            self.playback_peaks.reset(total=start // WAVE_SAMPLES)
        count = target - audio.tell()
        if count > 0:
            self.playback_peaks.extend(self.audio_envelope(audio.readframes(count)))
        self.draw_peaks(self.playback_canvas, self.playback_peaks, position=elapsed * rate / WAVE_SAMPLES)
        self.playback_time.configure(text=f'{timestamp(elapsed)} / {timestamp(self.playback_duration)}')
        if not self.seeking:
            self.playback_position.set(elapsed)

    def begin_seek(self, event=None):
        self.seeking = True

    def preview_seek(self, value):
        if self.seeking and self.selected:
            self.playback_time.configure(text=f'{timestamp(float(value))} / {timestamp(self.selected["duration"])}')

    def end_seek(self, event=None):
        position = self.playback_position.get()
        self.seeking = False
        if not self.selected or self.recording:
            return
        if self.playback_file is None:
            self.play()
        if self.playback_file is None:
            return
        try:
            position = max(0, min(position, max(0, self.playback_duration - 0.05)))
            self.player.play(position)
            self.playback_file.setpos(int(position * 16000))
            self.playback_peaks.reset(total=int(position * 100))
            self.playback_started = time.monotonic() - position
            self.playback_position.set(position)
            self.update_playback()
        except Exception as exc:
            self.stop_playback()
            messagebox.showerror('跳转失败', str(exc), parent=self)

    def open_folder(self):
        if self.selected:
            try:
                os.startfile(self.selected['folder'])
                self.feedback('已打开文件夹：' + self.selected['folder'])
            except OSError as exc:
                messagebox.showerror('无法打开', str(exc), parent=self)
        else:
            self.feedback('请先选择录音，再打开文件夹。')

    def retranscribe(self):
        if self.busy:
            self.feedback('已有任务正在处理，请等待完成。')
            return
        if self.recording:
            self.feedback('请先结束录音，再重新转写。')
            return
        if not self.selected:
            self.feedback('请先选择需要重新转写的录音。')
            return
        if not audio_path(self.selected['folder']).is_file():
            self.feedback('找不到录音文件，无法重新转写。')
            return
        self.process(self.selected, False, self.config_data)

    def retry(self):
        if self.busy:
            self.feedback('正在处理上一项任务，请等待完成。')
            return
        if self.recording:
            self.feedback('请先结束当前录音，再重新生成纪要。')
            return
        if not self.selected:
            self.feedback('请先选择需要重新生成纪要的录音。')
            return
        if not self.verified:
            self.feedback('请先到设置中验证 API 和模型，再重新生成纪要。')
            return
        if not self.flush_edits():
            return
        record = dict(self.selected)
        folder = Path(record['folder'])
        try:
            transcript = simplified((folder / 'transcript.txt').read_text(encoding='utf-8')).strip()
            if not transcript or transcript in ('等待本地转写…', '（未识别到语音）'):
                raise ValueError('当前原文为空或尚未完成转写，请先填写原文。')
        except (OSError, ValueError) as exc:
            self.feedback('无法生成纪要：' + str(exc))
            messagebox.showerror('无法生成纪要', str(exc), parent=self)
            return
        config = dict(self.config_data)
        self.busy = True
        self.processing_id = record['id']
        self.processing_kind = 'summary'
        self.import_button.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.retry_button.configure(state='disabled', text='正在生成纪要…')
        self.retranscribe_button.configure(state='disabled')
        self.set_editor_permissions()
        self.feedback('正在基于当前原文生成纪要：' + record['title'])
        self.progress.configure(mode='indeterminate')
        self.progress.start(15)
        def work():
            state = '纪要失败'
            try:
                result = summarize(config, transcript)
                state = '纪要保存失败'
                overwrite_text(folder / 'summary.md', result)
                state = '完成'
            except Exception as exc:
                logging.exception('重新生成纪要失败')
                self.events.put(('processing_error', (state, str(exc), config)))
            finally:
                self.events.put(('done', (record['id'], state)))
        self.background(work)

    def reset_waveform(self):
        self.peaks.reset()
        self.draw_waveform()

    @staticmethod
    def waveform_height(peak):
        # 对数刻度保留弱音细节，强音仍有层次；仅改变显示，不改变音频。
        return 0 if peak <= 0.0005 else min(124, 124 * math.log(peak / 0.0005) / math.log(2000))

    @staticmethod
    def audio_envelope(raw):
        values = array.array('h', raw)
        envelopes = []
        for start in range(0, len(values), WAVE_SAMPLES):
            chunk = values[start:start + WAVE_SAMPLES]
            rms = math.sqrt(sum(value * value for value in chunk) / len(chunk))
            peak = max(abs(value) for value in chunk)
            envelopes.append((rms * 0.7 + peak * 0.3) / 32768)
        return envelopes

    def draw_waveform(self):
        self.draw_peaks(self.canvas, self.peaks)

    def draw_peaks(self, canvas, peaks, position=None):
        width = max(canvas.winfo_width(), 100)
        center = max(canvas.winfo_height(), int(canvas['height'])) / 2
        count = min(WAVE_BARS, max(1, int(width / 3)))
        if not hasattr(canvas, 'wave_bars'):
            canvas.wave_center = canvas.create_line(0, center, width, center, fill='#cbd7ee')
            canvas.wave_bars = []
        if len(canvas.wave_bars) != count:
            for item in canvas.wave_bars:
                canvas.delete(item)
            canvas.wave_bars = [canvas.create_line(0, center, 0, center, fill='#3869db',
                                                  width=2, capstyle='round') for _ in range(count)]
        canvas.coords(canvas.wave_center, 0, center, width, center)
        values, total, updated = peaks.snapshot()
        if position is None:
            position = total
            if peaks is self.peaks and self.recording and not self.paused:
                position += min(1, max(0, (time.monotonic() - updated) / 0.01))
        # 默认 20ms 一根竖线，始终锚定在相同的绝对时间段。
        # 缩窄窗口时使用更大的固定分组，保持线间距离。
        step = math.ceil(WAVE_POINTS / count)
        completed = total // step
        # 所有竖线共用整数像素间距与同一份滚动偏移。
        # 若逐根计算小数坐标再由 Tk 取整，会交替出现宽窄间距，且随滚动漂移。
        pitch = max(3, width // count)
        origin = (width - count * pitch) // 2 + pitch // 2
        shift = round((position - completed * step) * pitch / step)
        first_sample = total - len(values)
        for i, item in enumerate(canvas.wave_bars):
            group_id = completed - count + i
            start = group_id * step - first_sample
            end = start + step
            # 不重算最左边已被截断的组，也不显示最右边尚未完成的组。
            group = values[start:end] if start >= 0 and end <= len(values) else []
            height = self.waveform_height(max(group, default=0)) * min(1, (center * 2 - 12) / 124) / 2
            x = origin + i * pitch - shift
            canvas.coords(item, x, center - height, x, center + height)
            canvas.itemconfigure(item, state='normal' if height else 'hidden')

    def animate_waveforms(self):
        started = time.perf_counter()
        if started + 0.001 < self.next_wave_frame:
            self.after(max(1, min(8, int((self.next_wave_frame - started) * 1000))), self.animate_waveforms)
            return
        period = WAVE_FRAME_MS / 1000
        # 使用累计截止时间，减少 Windows 定时器取整造成的长期帧率偏差。
        self.next_wave_frame = (started + period if started - self.next_wave_frame > period
                                else self.next_wave_frame + period)
        if self.canvas.winfo_ismapped():
            self.draw_waveform()
        self.update_playback()
        # 短等待配合帧截止时间，不修改系统定时器配置。
        delay = max(1, min(8, int((self.next_wave_frame - time.perf_counter()) * 1000)))
        self.after(delay, self.animate_waveforms)

    def tick(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.feedback(value)
                elif kind == 'imported':
                    self.finish_import(*value)
                elif kind == 'import_error':
                    self.busy = False
                    self.processing_kind = None
                    self.progress.stop()
                    self.progress.configure(mode='determinate', value=0)
                    for button in (self.start_button, self.import_button, self.retry_button, self.retranscribe_button):
                        button.configure(state='normal')
                    self.ai_check.configure(state='normal' if self.verified else 'disabled')
                    self.feedback('导入失败 · ' + value)
                    messagebox.showerror('导入失败', value, parent=self)
                elif kind == 'progress':
                    self.progress['value'] = value
                    self.feedback(f'正在本地转写 · {value:.0f}%')
                elif kind == 'models':
                    self.fetch_button.configure(state='normal', text='获取模型')
                    self.model_combo['values'] = value
                    self.status.set(f'已获取 {len(value)} 个模型，请选择后验证。' if value else '接口返回了空模型列表，可手动填写模型名称。')
                elif kind == 'models_error':
                    self.fetch_button.configure(state='normal', text='获取模型')
                    self.status.set('获取模型失败：' + value)
                    messagebox.showerror('获取模型失败', value, parent=self)
                elif kind == 'api_validation':
                    config, error = value
                    self.verifying = False
                    self.verify_button.configure(state='normal', text='验证 API 和模型')
                    if api_fingerprint(config) != api_fingerprint(self.config_data):
                        self.refresh_api_state()
                        self.status.set('验证期间 API 配置已更改，请验证当前配置。')
                        continue
                    if error:
                        clear_verification(config)
                        self.invalidate_api()
                        self.api_status.set('验证失败：' + error)
                        self.api_feedback.configure(foreground='#b33434')
                        self.status.set('API 和模型验证失败')
                        messagebox.showerror('API 验证失败', error, parent=self)
                    else:
                        try:
                            save_verification(config)
                            self.refresh_api_state()
                            self.status.set('API 验证成功，状态已保存，重启无需重复验证。')
                        except OSError as exc:
                            self.verified = True
                            self.api_status.set('验证成功，但保存验证状态失败：' + str(exc))
                            self.status.set('本次验证成功；持久化失败，重启后需重新验证。')
                            self.ai_check.configure(state='normal' if not self.recording else 'disabled')
                elif kind in ('error', 'processing_error'):
                    if kind == 'processing_error':
                        state, value, failed_config = value
                        if state == '纪要失败':
                            clear_verification(failed_config)
                            if api_fingerprint(failed_config) == api_fingerprint(self.config_data):
                                self.invalidate_api()
                                self.api_status.set('纪要请求失败，需重新验证：' + value)
                    self.feedback('处理失败 · ' + value)
                    messagebox.showerror('处理失败', value, parent=self)
                elif kind == 'done':
                    rid, state = value
                    for record in self.index:
                        if record['id'] == rid:
                            record['state'] = state
                    atomic_json(DATA / 'library.json', self.index)
                    summary_only = self.processing_kind == 'summary'
                    self.busy = False
                    self.processing_id = None
                    self.processing_kind = None
                    self.retry_button.configure(state='normal', text='重新生成纪要')
                    self.retranscribe_button.configure(state='normal', text='重新转写')
                    self.progress.stop()
                    self.progress.configure(mode='determinate')
                    self.start_button.configure(state='normal')
                    self.import_button.configure(state='normal')
                    self.ai_check.configure(state='normal' if self.verified else 'disabled')
                    self.progress['value'] = 100 if state == '完成' else 0
                    self.feedback(('纪要已更新 · 已覆盖保存，原文保持不变。' if summary_only else '处理完成 · 文件已保存，可预览或直接编辑。') if state == '完成' else state + ' · 请查看错误提示后重试。')
                    if self.selected and self.selected['id'] == rid:
                        self.select_record()
                    else:
                        self.set_editor_permissions()
        except queue.Empty:
            pass
        if self.recording:
            self.timer.configure(text=timestamp(self.samples / 16000))
        self.refresh_task_state()
        self.after(100, self.tick)

    def destroy(self):
        # 取消定时回调，避免窗口销毁后回调继续进入已释放的 Tcl 控件。
        for callback in self.tk.call('after', 'info'):
            self.after_cancel(callback)
        super().destroy()

    def close(self):
        if not self.flush_edits():
            messagebox.showerror('尚有未保存修改', '自动保存失败，请修复存储路径或权限后再退出，修改内容仍保留在编辑框中。', parent=self)
            return
        if self.recording:
            if not messagebox.askyesno('退出并丢弃录音？',
                    '当前正在录音（含暂停中的录音）。退出将中断并删除本次尚未保存的录音，是否退出？', parent=self):
                return
            try:
                self.stream.stop()
                self.stream.close()
                # 先停止音频回调，再通知写入线程结束，释放文件后删除。
                if self.writer.is_alive():
                    while self.writer.is_alive():
                        try:
                            self.audio_queue.put(None, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    self.writer.join()
                self.wav.close()
                self.pending.unlink(missing_ok=True)
                self.recording = False
                self.paused = False
            except Exception as exc:
                logging.exception('退出时清理录音失败')
                messagebox.showerror('无法退出', f'未能清理本次录音，请重试：{exc}', parent=self)
                return
        elif self.busy:
            messagebox.showinfo('正在处理录音', '录音已结束或已导入，正在转换音频、转写或生成纪要。请等待处理完成后退出。', parent=self)
            return
        self.stop_playback()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
