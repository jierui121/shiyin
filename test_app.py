"""离线回归：不使用真实麦克风、不发送外部 API 请求。"""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch, Mock
import time
import array
import math
import hashlib
import requests
from types import SimpleNamespace
from runtime_paths import runtime_paths
from model_store import ensure_model, MODEL_FILES
from audio_import import import_audio
from app import App, WaveHistory, WAVE_POINTS, WAVE_SAMPLES, WAVE_BARS, WAVE_FRAME_MS
from core import (ASR_MODELS, atomic_json, protect, summarize, timestamp, simplified, summary_prompt,
                  load_config, DAILY_PROMPT, load_verification, save_verification)


class AppTests(unittest.TestCase):
    def test_delete_record_confirmation_and_all_note_files(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                folder = Path(temp) / '0'
                (folder / 'audio.mp3').write_bytes(b'test mp3')
                with patch('app.messagebox.askyesno', return_value=False):
                    app.delete_selected()
                self.assertTrue(folder.exists())
                self.assertEqual(len(app.index), 2)
                with patch('app.messagebox.askyesno', return_value=True), patch('core.atomic_json') as save:
                    app.delete_selected()
                    self.assertEqual([r['id'] for r in save.call_args.args[1]], ['1'])
                self.assertFalse(folder.exists())
                self.assertEqual(app.selected['id'], '1')
                self.assertEqual(len(app.index), 1)
                self.assertEqual(app.dirty_editors, set())
            finally:
                app.destroy()

    def test_delete_rejects_processing_and_restores_on_index_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                app.processing_id = '0'
                with patch('app.messagebox.askyesno') as confirm:
                    app.delete_selected()
                    confirm.assert_not_called()
                app.processing_id = None
                with patch('app.messagebox.askyesno', return_value=True), \
                     patch('core.atomic_json', side_effect=OSError('模拟索引写入失败')), patch('app.messagebox.showerror'):
                    app.delete_selected()
                self.assertEqual(len(app.index), 2)
                self.assertEqual((Path(temp) / '0' / 'summary.md').read_text(encoding='utf-8'), '纪要0')
                self.assertEqual(list(Path(temp).rglob('.deleting-*')), [])
            finally:
                app.destroy()

    def test_delete_last_record_preserves_unrelated_files_and_clears_preview(self):
        from core import delete_record
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                app.index = app.index[:1]
                app.refresh_library(True)
                folder = Path(temp) / '0'
                (folder / 'personal.txt').write_text('保留', encoding='utf-8')
                with patch('app.messagebox.askyesno', return_value=True), patch('core.atomic_json'):
                    app.delete_selected()
                self.assertEqual(app.index, [])
                self.assertIsNone(app.selected)
                self.assertEqual(app.editor_files, {})
                self.assertEqual(app.transcript.get('1.0', 'end-1c'), '')
                self.assertTrue((folder / 'personal.txt').exists())
                self.assertFalse((folder / 'summary.md').exists())
                self.assertTrue(app.library_all_dates)
                with self.assertRaises(ValueError):
                    delete_record(dict(id='a', folder=str(folder)), [dict(id='b', folder=str(folder))], Path(temp) / 'index.json')
            finally:
                app.destroy()

    def test_close_recording_cancel_and_confirm_discards_audio(self):
        for paused in (False, True):
            with self.subTest(paused=paused), tempfile.TemporaryDirectory() as temp, \
                 patch('app.read_index', return_value=[]), patch('app.DATA', Path(temp)):
                app = App()
                class Stream:
                    def __init__(self, **kwargs):
                        self.callback = kwargs['callback']
                        self.stopped = self.closed = False
                    def start(self):
                        self.callback(b'\0\0' * 1600, 1600, None, None)
                    def stop(self):
                        self.stopped = True
                    def close(self):
                        self.closed = True
                try:
                    with patch('sounddevice.RawInputStream', Stream):
                        app.start_recording()
                    if paused:
                        app.pause()
                    with patch('app.messagebox.askyesno', return_value=False), patch.object(app, 'destroy') as destroy:
                        app.close()
                        destroy.assert_not_called()
                    self.assertTrue(app.recording)
                    self.assertTrue(app.pending.exists())
                    self.assertFalse(app.stream.stopped)
                    with patch('app.messagebox.askyesno', return_value=True), \
                         patch.object(app, 'destroy') as destroy, patch('app.atomic_json') as index:
                        app.close()
                        destroy.assert_called_once()
                        index.assert_not_called()
                    self.assertFalse(app.recording)
                    self.assertFalse(app.writer.is_alive())
                    self.assertTrue(app.stream.closed)
                    self.assertFalse(app.pending.exists())
                    self.assertEqual(app.index, [])
                finally:
                    app.destroy()

    def test_processing_close_uses_processing_message(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.busy = True
            app.processing_kind = 'import'
            app.refresh_task_state()
            self.assertEqual(app.task_state.get(), '处理中')
            with patch('app.messagebox.showinfo') as info, \
                 patch('app.messagebox.askyesno') as confirm, patch.object(app, 'destroy') as destroy:
                app.close()
                self.assertEqual(info.call_args.args[0], '正在处理录音')
                confirm.assert_not_called()
                destroy.assert_not_called()
        finally:
            app.destroy()

    def test_mp3_seek_forward_backward_and_stopped(self):
        from audio_media import encode_mp3, PreviewAudio, NativePlayer
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            root = Path(temp)
            with wave.open(str(root / 'source.wav'), 'wb') as source:
                source.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                source.writeframes(b'\0\x10' * 16000 * 8)
            encode_mp3(root / 'source.wav', root / 'audio.mp3')
            reader = PreviewAudio(root / 'audio.mp3')
            try:
                reader.setpos(16000 * 5)
                self.assertEqual(len(reader.readframes(1600)), 3200)
                reader.setpos(16000)
                self.assertEqual(len(reader.readframes(1600)), 3200)
            finally:
                reader.close()
            app = App()
            try:
                app.selected = dict(folder=temp, title='拖动测试', id='seek', duration=8)
                with patch('app.NativePlayer') as player:
                    app.play()
                    for position in (5, 1):
                        app.begin_seek()
                        app.playback_position.set(position)
                        app.end_seek()
                        player.return_value.play.assert_called_with(position)
                        self.assertAlmostEqual(app.playback_position.get(), position, delta=0.1)
                    app.stop_playback()
                    app.begin_seek()
                    app.playback_position.set(3)
                    app.end_seek()
                    player.return_value.play.assert_called_with(3)
            finally:
                app.stop_playback()
                app.destroy()
            with patch.object(NativePlayer, 'command', return_value='') as command:
                player = NativePlayer(root / 'audio.mp3')
                player.play(2.5)
                self.assertIn('from 2500', command.call_args.args[0])
                player.close()

    def test_import_compressed_formats(self):
        import av
        import numpy as np
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for extension, codec in [('mp3', 'libmp3lame'), ('m4a', 'aac'), ('flac', 'flac'), ('ogg', 'libvorbis')]:
                with self.subTest(format=extension):
                    source = root / ('录音.' + extension)
                    with av.open(str(source), 'w') as output:
                        stream = output.add_stream(codec, rate=48000)
                        stream.layout = 'mono'
                        signal = (0.2 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype('float32')
                        frame = av.AudioFrame.from_ndarray(signal.reshape(1, -1), format='fltp', layout='mono')
                        frame.sample_rate = 48000
                        for packet in stream.encode(frame):
                            output.mux(packet)
                        for packet in stream.encode(None):
                            output.mux(packet)
                    record = import_audio(source, root / 'notes', '2026-10-26')
                    self.assertAlmostEqual(record['duration'], 1, delta=0.1)

    def test_import_stereo_audio_and_name_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / '中文录音.wav'
            with wave.open(str(source), 'wb') as audio:
                audio.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
                audio.writeframes(array.array('h', [1000, -500] * 44100).tobytes())
            original = source.read_bytes()
            storage = root / 'notes'
            storage.mkdir()
            (storage / source.stem).mkdir()
            messages = []
            record = import_audio(source, storage, '2026-10-26', True, messages.append)
            self.assertEqual(record['title'], '中文录音_2')
            self.assertEqual(record['date'], '2026-10-26')
            self.assertAlmostEqual(record['duration'], 1, places=2)
            self.assertEqual(source.read_bytes(), original)
            from audio_media import PreviewAudio
            audio = PreviewAudio(Path(record['folder']) / 'audio.mp3')
            self.assertEqual(audio.getframerate(), 16000)
            self.assertAlmostEqual(audio.duration, 1, delta=0.15)
            audio.close()
            self.assertTrue(messages)
            self.assertIn('等待生成', (Path(record['folder']) / 'summary.md').read_text(encoding='utf-8'))

    def test_import_invalid_audio_cleans_only_new_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'broken.mp3'
            source.write_bytes(b'not audio')
            storage = root / 'notes'
            storage.mkdir()
            existing = storage / 'broken'
            existing.mkdir()
            with self.assertRaises(Exception):
                import_audio(source, storage, '2026-10-26')
            self.assertEqual(list(storage.iterdir()), [existing])
            self.assertTrue(source.exists())

    def test_import_runs_transcription_and_summary(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            root = Path(temp)
            source = root / 'meeting.wav'
            with wave.open(str(source), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\0\0' * 16000)
            app = App()
            try:
                app.config_data['storage'] = str(root / 'notes')
                app.verified = True
                app.ai_enabled.set(True)
                app.model_name = app.config_data['asr_model']
                app.model = SimpleNamespace(transcribe=Mock(return_value=(iter([
                    SimpleNamespace(start=0, end=1, text='导入测试原文')]), None)))
                jobs = []
                with patch('app.filedialog.askopenfilename', return_value=str(source)), \
                     patch.object(app, 'background', side_effect=jobs.append), patch('app.atomic_json'):
                    app.import_recording()
                    self.assertTrue(app.busy)
                    self.assertFalse(app.recording)
                    self.assertEqual(app.task_state.get(), '处理中')
                    self.assertIn('disabled', app.import_button.state())
                    jobs.pop(0)()
                    app.tick()
                    self.assertEqual(app.processing_kind, 'transcribe')
                    with patch('app.summarize', return_value='导入测试纪要') as summary:
                        jobs.pop(0)()
                        summary.assert_called_once()
                    app.tick()
                folder = Path(app.index[0]['folder'])
                self.assertIn('导入测试原文', (folder / 'transcript.txt').read_text(encoding='utf-8'))
                self.assertEqual((folder / 'summary.md').read_text(encoding='utf-8'), '导入测试纪要')
                self.assertFalse(app.busy)
                self.assertEqual(app.task_state.get(), '就绪')
                self.assertNotIn('disabled', app.import_button.state())
            finally:
                app.destroy()

    def test_independent_retranscribe_only_changes_transcript(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                (Path(temp) / '0' / 'audio.wav').write_bytes(b'fake audio for mocked model')
                app.verified = False
                app.ai_enabled.set(True)
                app.model_name = app.config_data['asr_model']
                app.model = SimpleNamespace(transcribe=Mock(return_value=(iter([SimpleNamespace(start=0, end=1, text='新的轉寫')]), None)))
                queued = []
                with patch.object(app, 'background', side_effect=queued.append):
                    app.retranscribe()
                self.assertTrue(app.busy)
                self.assertIn('disabled', app.retranscribe_button.state())
                self.assertIn('disabled', app.retry_button.state())
                with patch('app.summarize') as summary:
                    queued[0]()
                    summary.assert_not_called()
                with patch('app.atomic_json'):
                    app.tick()
                self.assertIn('新的转写', (Path(temp) / '0' / 'transcript.txt').read_text(encoding='utf-8'))
                self.assertEqual((Path(temp) / '0' / 'summary.md').read_text(encoding='utf-8'), '纪要0')
                self.assertNotIn('disabled', app.retranscribe_button.state())
                self.assertFalse(app.busy)
            finally:
                app.destroy()

    def test_model_store_uses_local_files_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'medium'
            folder.mkdir()
            for name in MODEL_FILES:
                (folder / name).write_bytes(b'test')
            with patch('model_store.requests.get') as network:
                self.assertEqual(ensure_model('medium', root, root), folder)
                network.assert_not_called()

    def test_model_download_falls_back_and_checks_hashes(self):
        files = {name: ('test ' + name).encode() for name in MODEL_FILES}
        entries = []
        for name, content in files.items():
            entry = dict(rfilename=name, size=len(content), blobId=hashlib.sha1(f'blob {len(content)}\0'.encode()+content).hexdigest())
            if name == 'model.bin':
                entry['lfs'] = {'sha256': hashlib.sha256(content).hexdigest()}
            entries.append(entry)
        class Response:
            status_code = 200
            def __init__(self, body=None):
                self.body = body
            def raise_for_status(self):
                pass
            def json(self):
                return dict(sha='test-revision', siblings=entries)
            def iter_content(self, size):
                yield self.body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        def get(url, **kwargs):
            if url.startswith('https://huggingface.co'):
                raise requests.ConnectTimeout('模拟官方站点超时')
            return Response() if '/api/' in url else Response(files[url.rsplit('/', 1)[-1]])
        with tempfile.TemporaryDirectory() as temp, patch('model_store.requests.get', side_effect=get):
            root = Path(temp)
            messages = []
            folder = ensure_model('medium', root / 'bundle', root / 'cache', messages.append)
            self.assertTrue(any('备用下载' in message for message in messages))
            for name, content in files.items():
                self.assertEqual((folder / name).read_bytes(), content)
            self.assertEqual(list(folder.glob('*.part')), [])

    def test_packaged_paths_keep_data_outside_application(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch('runtime_paths.sys.frozen', True, create=True), \
                 patch('runtime_paths.sys.executable', str(base / 'app' / 'Shiyin.exe')), \
                 patch.dict('runtime_paths.os.environ', {'LOCALAPPDATA': str(base / 'profile')}):
                root, data = runtime_paths()
                self.assertEqual(root, (base / 'app').resolve())
                self.assertEqual(data, base / 'profile' / 'Shiyin' / 'data')
    def test_button_feedback_for_empty_selection_and_model_fetch(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.retry()
            self.assertIn('请先选择', app.library_status.get())
            app.play()
            self.assertIn('请先选择', app.library_status.get())
            app.stop_playback(notify=True)
            self.assertIn('已停止', app.library_status.get())
            with patch.object(app, 'background', side_effect=lambda work: work()), \
                 patch('app.api', return_value={'data': [{'id': 'test-model'}]}):
                app.fetch_models()
                self.assertIn('disabled', app.fetch_button.state())
                app.tick()
                self.assertNotIn('disabled', app.fetch_button.state())
                self.assertIn('已获取 1', app.status.get())
            with patch.object(app, 'background', side_effect=lambda work: work()), \
                 patch('app.api', side_effect=ValueError('模拟网络错误')), patch('app.messagebox.showerror') as popup:
                app.fetch_models()
                app.tick()
                self.assertNotIn('disabled', app.fetch_button.state())
                self.assertIn('获取模型失败', app.status.get())
                popup.assert_called_once()
        finally:
            app.destroy()

    @staticmethod
    def editable_library(app, root):
        app.index = []
        for i in range(2):
            folder = Path(root) / str(i)
            folder.mkdir()
            (folder / 'transcript.txt').write_text('原文' + str(i), encoding='utf-8')
            (folder / 'summary.md').write_text('纪要' + str(i), encoding='utf-8')
            app.index.append(dict(id=str(i), title=f'录音{i}', folder=str(folder), date='2026-10-26',
                                  created=f'2026-10-26T12:00:0{i}', duration=1, state='完成'))
        app.date.set('2026-10-26')
        app.refresh_library()

    def test_edit_autosaves_both_files_and_flushes_on_selection(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                for widget in (app.summary, app.transcript):
                    self.assertEqual(str(widget['state']), 'normal')
                    widget.delete('1.0', 'end')
                    widget.insert('1.0', '手动编辑：中文 API\n第二行')
                app.update()
                app.after(350, app.quit)
                app.mainloop()
                for name in ('transcript.txt', 'summary.md'):
                    self.assertEqual((Path(temp) / '0' / name).read_text(encoding='utf-8'), '手动编辑：中文 API\n第二行')
                self.assertIn('已自动保存', app.editor_feedback[app.summary].get())
                app.transcript.insert('end', '最后一笔')
                app.records.current(1)
                app.select_record()
                self.assertIn('最后一笔', (Path(temp) / '0' / 'transcript.txt').read_text(encoding='utf-8'))
                self.assertEqual((Path(temp) / '1' / 'transcript.txt').read_text(encoding='utf-8'), '原文1')
                self.assertEqual(app.selected['id'], '1')
                self.assertEqual(list(Path(temp).rglob('*.saving')), [])
            finally:
                app.destroy()

    def test_save_failure_preserves_edits_and_blocks_switch_and_close(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                app.summary.insert('end', '未保存修改')
                with patch('app.overwrite_text', side_effect=OSError('模拟写入失败')):
                    self.assertFalse(app.flush_edits())
                    app.records.current(1)
                    app.select_record()
                    self.assertEqual(app.selected['id'], '0')
                    self.assertIn('未保存修改', app.summary.get('1.0', 'end'))
                    with patch('app.messagebox.showerror') as popup:
                        app.close()
                        popup.assert_called_once()
                        self.assertTrue(app.winfo_exists())
                self.assertTrue(app.flush_edits())
                self.assertIn('未保存修改', (Path(temp) / '0' / 'summary.md').read_text(encoding='utf-8'))
            finally:
                app.destroy()

    def test_retry_feedback_locks_target_and_keeps_other_note_edits(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                queued = []
                app.verified = True
                app.ai_enabled.set(False)
                app.model = SimpleNamespace(transcribe=Mock(side_effect=AssertionError('不应调用转写模型')))
                app.transcript.insert('end', '，使用手动修订后的原文')
                with patch.object(app, 'background', side_effect=queued.append):
                    app.retry()
                self.assertTrue(app.busy)
                self.assertIn('正在', app.library_status.get())
                self.assertIn('disabled', app.retry_button.state())
                self.assertEqual(str(app.transcript['state']), 'disabled')
                app.records.current(1)
                app.select_record()
                self.assertEqual(str(app.transcript['state']), 'normal')
                app.transcript.insert('end', '另一条的修改')
                with patch('app.summarize', return_value='重新生成的纪要') as generate:
                    queued[0]()
                    self.assertEqual(generate.call_args.args[1], '原文0，使用手动修订后的原文')
                app.model.transcribe.assert_not_called()
                with patch('app.atomic_json'):
                    app.tick()
                self.assertFalse(app.busy)
                self.assertNotIn('disabled', app.retry_button.state())
                self.assertIn('另一条的修改', app.transcript.get('1.0', 'end'))
                app.flush_edits()
                self.assertEqual((Path(temp) / '0' / 'transcript.txt').read_text(encoding='utf-8'), '原文0，使用手动修订后的原文')
                self.assertEqual((Path(temp) / '0' / 'summary.md').read_text(encoding='utf-8'), '重新生成的纪要')
            finally:
                app.destroy()

    def test_regenerate_requires_verified_api_and_preserves_old_summary_on_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                self.editable_library(app, temp)
                app.verified = False
                with patch.object(app, 'background') as work:
                    app.retry()
                    work.assert_not_called()
                self.assertIn('验证', app.library_status.get())
                app.verified = True
                with patch.object(app, 'background', side_effect=lambda work: work()), \
                     patch('app.summarize', side_effect=ValueError('模拟失败')), \
                     patch('app.clear_verification'), patch('app.atomic_json'), patch('app.messagebox.showerror'):
                    app.retry()
                    app.tick()
                self.assertFalse(app.busy)
                self.assertEqual((Path(temp) / '0' / 'summary.md').read_text(encoding='utf-8'), '纪要0')
                self.assertEqual((Path(temp) / '0' / 'transcript.txt').read_text(encoding='utf-8'), '原文0')
                app.verified = True
                app.transcript.delete('1.0', 'end')
                with patch('app.messagebox.showerror'), patch.object(app, 'background') as work:
                    app.retry()
                    work.assert_not_called()
                self.assertIn('原文为空', app.library_status.get())
            finally:
                app.destroy()

    def test_api_verification_feedback_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as temp, patch('core.DATA', Path(temp)), patch('app.read_index', return_value=[]):
            app = App()
            reopened = None
            try:
                for key, value in dict(base_url='https://example.test/v1', key='mock-key', model='test').items():
                    app.fields[key].set(value)
                app.save_settings()
                queued = []
                with patch.object(app, 'background', side_effect=queued.append), patch('app.summarize', return_value='连接成功'):
                    app.verify_api()
                    self.assertTrue(app.verifying)
                    self.assertIn('正在验证', app.api_status.get())
                    # 验证期间切换并保存纪要风格，不应让同一 API 的验证结果消失。
                    app.style_label.set('日常记录')
                    app.change_summary_style()
                    app.save_settings()
                    queued[0]()
                    app.tick()
                self.assertTrue(app.verified)
                self.assertFalse(app.verifying)
                self.assertIn('已验证', app.api_status.get())
                self.assertNotIn('disabled', app.ai_check.state())
                self.assertIsNotNone(load_verification(app.config_data))
                reopened = App()
                self.assertTrue(reopened.verified)
                self.assertIn('上次成功', reopened.api_status.get())
                reopened.fields['model'].set('other-model')
                reopened.save_settings()
                self.assertFalse(reopened.verified)
            finally:
                if reopened is not None:
                    reopened.destroy()
                app.destroy()

    def test_failed_verification_clears_success_and_shows_reason(self):
        with tempfile.TemporaryDirectory() as temp, patch('core.DATA', Path(temp)), patch('app.read_index', return_value=[]):
            app = App()
            try:
                for key, value in dict(base_url='https://example.test/v1', key='mock-key', model='test').items():
                    app.fields[key].set(value)
                app.save_settings()
                save_verification(app.config_data)
                app.refresh_api_state()
                self.assertTrue(app.verified)
                with patch.object(app, 'background', side_effect=lambda work: work()), \
                     patch('app.summarize', side_effect=ValueError('模拟额度不足')), patch('app.messagebox.showerror') as popup:
                    app.verify_api()
                    app.tick()
                    self.assertFalse(app.verified)
                    self.assertFalse(app.verifying)
                    self.assertIn('模拟额度不足', app.api_status.get())
                    self.assertIsNone(load_verification(app.config_data))
                    popup.assert_called_once()
                self.assertNotIn('disabled', app.verify_button.state())
            finally:
                app.destroy()

    def test_checkmark_toggles_and_respects_disabled_state(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            self.assertEqual(str(app.ai_check['style']), 'Tick.TCheckbutton')
            app.ai_check.configure(state='normal')
            app.ai_check.invoke()
            self.assertTrue(app.ai_enabled.get())
            self.assertIn('selected', app.ai_check.state())
            self.assertEqual(app.check_images[1].get(8, 14), (255, 255, 255))
            self.assertEqual(app.check_images[1].get(5, 5), (56, 105, 219))
            app.ai_check.configure(state='disabled')
            app.ai_check.invoke()
            self.assertTrue(app.ai_enabled.get())
        finally:
            app.destroy()

    def test_edit_record_date_persists_and_keeps_selection(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.index = [dict(id=str(i), title=f'test{i}', date='2026-10-25', created='2026-10-25T12:00:00',
                              duration=1, folder='missing-test-folder', state='完成') for i in range(2)]
            app.date.set('2026-10-25')
            app.refresh_library()
            with patch('app.atomic_json') as save:
                self.assertTrue(app.change_record_date('1', '2026-10-01'))
                self.assertEqual(app.index[0]['date'], '2026-10-25')
                self.assertEqual(app.index[1]['date'], '2026-10-01')
                self.assertEqual(app.index[1]['created'], '2026-10-25T12:00:00')
                self.assertEqual(app.selected['id'], '1')
                self.assertEqual(app.date.get(), '2026-10-01')
                self.assertEqual(save.call_args.args[1][1]['date'], '2026-10-01')
            with patch('app.atomic_json', side_effect=OSError('模拟失败')), patch('app.messagebox.showerror'):
                self.assertFalse(app.change_record_date('1', '2026-09-01'))
                self.assertEqual(app.index[1]['date'], '2026-10-01')
        finally:
            app.destroy()

    def test_record_calendar_does_not_change_library_filter(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            original = app.date.get()
            app.record_date.set('2026-10-26')
            app.show_calendar(app.record_date)
            window = [w for w in app.winfo_children() if w.winfo_class() == 'Toplevel'][0]
            grid = window.winfo_children()[1]
            first = next(w for w in grid.winfo_children() if w.winfo_class() == 'TButton' and str(w.cget('text')) == '1')
            first.invoke()
            self.assertEqual(app.record_date.get(), '2026-10-01')
            self.assertEqual(app.date.get(), original)
        finally:
            app.destroy()

    def test_simplified_conversion_and_old_settings_migration(self):
        self.assertEqual(simplified('[00:00:01] 會議記錄：學習軟體 API 30 FPS。'), '[00:00:01] 会议记录：学习软体 API 30 FPS。')
        with tempfile.TemporaryDirectory() as temp, patch('core.DATA', Path(temp)):
            atomic_json(Path(temp) / 'settings.json', {'prompt': '原有会议格式', 'key': ''})
            config = load_config()
            self.assertEqual(config['summary_style'], 'meeting')
            self.assertEqual(config['meeting_prompt'], '原有会议格式')
            self.assertEqual(summary_prompt(config), '原有会议格式')

    def test_summary_styles_lock_preserve_custom_and_discard(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.update()
            app.tabs.select(app.settings_page)
            app.update()
            original = dict(app.config_data)
            app.style_label.set('会议纪要')
            app.change_summary_style()
            self.assertEqual(str(app.prompt['state']), 'disabled')
            self.assertEqual(app.prompt.get('1.0', 'end-1c'), original['meeting_prompt'])
            app.style_label.set('自定义')
            app.change_summary_style()
            self.assertEqual(str(app.prompt['state']), 'normal')
            app.prompt.delete('1.0', 'end')
            app.prompt.insert('1.0', '只写一句话。')
            app.style_label.set('日常记录')
            app.change_summary_style()
            self.assertEqual(str(app.prompt['state']), 'disabled')
            self.assertEqual(app.settings_snapshot()['prompt'], DAILY_PROMPT)
            app.style_label.set('自定义')
            app.change_summary_style()
            self.assertEqual(app.prompt.get('1.0', 'end-1c'), '只写一句话。')
            self.assertEqual(app.settings_snapshot()['custom_prompt'], '只写一句话。')
            app.tabs.select(0)
            app.update()
            self.assertEqual(app.settings_snapshot(), original)
        finally:
            app.destroy()

    def test_summary_style_is_used_in_api_and_output_is_simplified(self):
        config = dict(key='mock', model='test', prompt='旧格式', summary_style='daily', custom_prompt='自定义格式')
        result = {'choices': [{'finish_reason': 'stop', 'message': {'content': '今天學習了新知識。'}}]}
        with patch('core.api', return_value=result) as request:
            self.assertEqual(summarize(config, '原文'), '今天学习了新知识。')
            system = request.call_args.args[2]['messages'][0]['content']
            self.assertIn(DAILY_PROMPT, system)
            self.assertNotIn('旧格式', system)
            self.assertNotIn('自定义格式', system)
        config['summary_style'] = 'custom'
        self.assertEqual(summary_prompt(config), '自定义格式')

    def test_wave_history_keeps_heights_while_scrolling(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.tabs.select(1)
            app.update()
            canvas = app.playback_canvas
            history = WaveHistory()
            history.extend([0.004 + (i % 11) * 0.003 for i in range(WAVE_POINTS)])
            def frame(position=None):
                app.draw_peaks(canvas, history, position=position)
                step = math.ceil(WAVE_POINTS / len(canvas.wave_bars))
                completed = history.total // step
                result = {}
                for i, item in enumerate(canvas.wave_bars):
                    if canvas.itemcget(item, 'state') == 'normal':
                        x, top, _, bottom = canvas.coords(item)
                        result[completed - len(canvas.wave_bars) + i] = (x, bottom - top)
                return result
            previous = frame()
            for _ in range(12):
                history.append(0.02)
                current = frame()
                for key in previous.keys() & current.keys():
                    self.assertAlmostEqual(previous[key][1], current[key][1])
                    self.assertLessEqual(current[key][0], previous[key][0])
                previous = current
            a = frame(history.total + 0.2)
            b = frame(history.total + 0.7)
            for key in a.keys() & b.keys():
                self.assertAlmostEqual(a[key][1], b[key][1])
                # 整体按整数像素平移，所有竖线共享同一偏移，避免间隙漂移。
                self.assertGreaterEqual(a[key][0] - b[key][0], 0)
            shifts = {a[key][0] - b[key][0] for key in a.keys() & b.keys()}
            self.assertEqual(len(shifts), 1)
        finally:
            app.destroy()

    def test_wave_spacing_stays_uniform_across_scroll_phases(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.tabs.select(1)
            app.update()
            canvas = app.playback_canvas
            history = WaveHistory()
            history.extend([0.2] * WAVE_POINTS)
            for width in (853, 1001, 1034, 1279):
                with patch.object(canvas, 'winfo_width', return_value=width):
                    for phase in (0, 0.13, 0.51, 0.97, 1.2, 1.89):
                        app.draw_peaks(canvas, history, position=history.total + phase)
                        xs = [canvas.coords(item)[0] for item in canvas.wave_bars]
                        self.assertTrue(all(x == round(x) for x in xs))
                        gaps = {b - a for a, b in zip(xs, xs[1:])}
                        self.assertEqual(len(gaps), 1, (width, phase, gaps))
                        self.assertGreater(min(gaps), 2)
        finally:
            app.destroy()

    def test_dense_envelope_preserves_amplitude_levels(self):
        raw = array.array('h', [value for level in (0, 300, 3000, 12000, 30000)
                               for value in [level] * WAVE_SAMPLES]).tobytes()
        levels = App.audio_envelope(raw)
        self.assertEqual(len(levels), 5)
        heights = [App.waveform_height(level) for level in levels]
        self.assertEqual(heights[0], 0)
        self.assertEqual(heights, sorted(set(heights)))
        self.assertLess(heights[-1], 124)

    def test_preview_layout_and_compact_selector(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.tabs.select(1)
            app.update()
            self.assertEqual(app.records.winfo_class(), 'TCombobox')
            self.assertEqual(str(app.records['state']), 'readonly')
            self.assertGreater(app.transcript.winfo_width(), app.summary.winfo_width() * 2)
            self.assertGreater(app.transcript.winfo_width() + app.summary.winfo_width(), app.winfo_width() * 0.85)
            self.assertEqual(app.transcript.winfo_rooty(), app.summary.winfo_rooty())
            self.assertGreater(app.transcript.winfo_rootx(), app.summary.winfo_rootx())
            self.assertLess(app.records.winfo_height(), 40)
            app.draw_peaks(app.playback_canvas, app.playback_peaks)
            items = app.playback_canvas.find_all()
            for i in range(10):
                app.playback_peaks.append(i / 100)
                app.draw_peaks(app.playback_canvas, app.playback_peaks)
            self.assertEqual(app.playback_canvas.find_all(), items)
            self.assertEqual(len(app.playback_peaks), WAVE_POINTS)
            self.assertAlmostEqual(1000 / WAVE_FRAME_MS, 30)
            bars = app.playback_canvas.wave_bars
            self.assertLessEqual(len(bars), WAVE_BARS)
            self.assertEqual(len(bars), 320)
            self.assertTrue(all(app.playback_canvas.type(item) == 'line' for item in items))
            first_x = app.playback_canvas.coords(bars[0])[0]
            second_x = app.playback_canvas.coords(bars[1])[0]
            self.assertGreater(second_x - first_x, float(app.playback_canvas.itemcget(bars[0], 'width')))
        finally:
            app.destroy()

    def test_record_pause_save(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                app.config_data['storage'] = temp
                app.record_date.set('2026-10-01')
                class Stream:
                    def __init__(self, **kwargs):
                        self.callback = kwargs['callback']
                    def start(self):
                        self.callback(b'\0\0' * 1600, 1600, None, None)
                    def stop(self):
                        pass
                    def close(self):
                        pass
                with patch('sounddevice.RawInputStream', Stream), \
                     patch('app.filedialog.asksaveasfilename', return_value=str(Path(temp) / 'sample.wav')), \
                     patch('app.atomic_json'), patch.object(app, 'process') as process, \
                     patch.object(app, 'background', side_effect=lambda job: job()):
                    app.start_recording()
                    self.assertTrue(app.recording)
                    app.pause()
                    app.stream.callback(b'\0\0' * 1600, 1600, None, None)
                    app.pause()
                    app.stream.callback(b'\0\0' * 1600, 1600, None, None)
                    app.stop_recording()
                    app.tick()
                    from audio_media import PreviewAudio
                    audio = PreviewAudio(Path(temp) / 'sample' / 'audio.mp3')
                    self.assertAlmostEqual(audio.duration, 0.2, delta=0.15)
                    audio.close()
                    self.assertEqual(len(app.index), 1)
                    self.assertEqual(app.index[0]['date'], '2026-10-01')
                    self.assertEqual(app.index[0]['created'][:10], time.strftime('%Y-%m-%d'))
                    self.assertTrue((Path(temp) / 'sample' / 'transcript.txt').exists())
                    self.assertTrue((Path(temp) / 'sample' / 'summary.md').exists())
                    process.assert_called_once()
                    self.assertTrue(all(value == 0 for value in app.peaks))
            finally:
                app.destroy()

    def test_dpapi(self):
        self.assertEqual(protect(protect('中文-test-key'), True), '中文-test-key')

    def test_cancel_does_not_save_or_transcribe(self):
        with tempfile.TemporaryDirectory() as temp, patch('app.read_index', return_value=[]):
            app = App()
            try:
                app.config_data['storage'] = temp
                class Stream:
                    def __init__(self, **kwargs):
                        self.callback = kwargs['callback']
                    def start(self):
                        self.callback(b'\0\1' * 1600, 1600, None, None)
                    def stop(self):
                        pass
                    def close(self):
                        pass
                with patch('sounddevice.RawInputStream', Stream), \
                     patch('app.filedialog.asksaveasfilename', return_value=''), \
                     patch('app.atomic_json') as write_index, patch.object(app, 'process') as process:
                    app.peaks.append(0.9)
                    app.start_recording()
                    self.assertNotIn(0.9, app.peaks)
                    app.stop_recording()
                    self.assertFalse(app.pending.exists())
                    self.assertEqual(list(Path(temp).iterdir()), [])
                    self.assertEqual(app.index, [])
                    self.assertTrue(all(value == 0 for value in app.peaks))
                    process.assert_not_called()
                    write_index.assert_not_called()
            finally:
                app.destroy()

    def test_settings_discard_and_explicit_save(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.update()
            app.tabs.select(app.settings_page)
            app.update()
            original = dict(app.config_data)
            with patch('app.save_config') as save:
                app.fields['base_url'].set('https://unsaved.example/v1')
                app.fields['storage'].set('unsaved-folder')
                app.fields['asr_model'].set('tiny')
                app.prompt.insert('end', '未保存')
                app.tabs.select(0)
                app.update()
                self.assertEqual(app.settings_snapshot(), original)
                self.assertEqual(app.config_data, original)
                save.assert_not_called()
                with tempfile.TemporaryDirectory() as temp:
                    app.tabs.select(app.settings_page)
                    app.update()
                    app.fields['storage'].set(temp)
                    app.fields['asr_model'].set('base')
                    app.save_settings()
                    app.tabs.select(0)
                    app.update()
                    self.assertEqual(app.fields['storage'].get(), temp)
                    self.assertEqual(app.fields['asr_model'].get(), 'base')
                    save.assert_called_once()
            selectors = [w for w in app.settings_page.winfo_children()
                         if w.winfo_class() == 'TCombobox' and str(w.cget('state')) == 'readonly']
            self.assertEqual(tuple(selectors[0]['values']), ASR_MODELS)
        finally:
            app.destroy()

    def test_waveform_and_playback(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.update()
            self.assertEqual(app.waveform_height(0), 0)
            self.assertGreater(app.waveform_height(0.01), 120 * 0.01 * 10)
            with tempfile.TemporaryDirectory() as temp:
                with wave.open(str(Path(temp) / 'audio.wav'), 'wb') as audio:
                    audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    audio.writeframes(b'\0\x10' * 16000)
                app.selected = {'folder': temp, 'title': '测试录音', 'id': 'test'}
                with patch('app.NativePlayer'), patch('app.time.monotonic', return_value=100):
                    app.play()
                    self.assertIsNotNone(app.playback_file)
                with patch('app.time.monotonic', return_value=100.2):
                    app.update_playback()
                    self.assertGreater(max(app.playback_peaks), 0)
                    self.assertTrue(app.playback_canvas.find_all())
                    previous = list(app.playback_peaks)
                    app.update_playback()
                    self.assertEqual(list(app.playback_peaks), previous)
                with patch('app.time.monotonic', return_value=101.1):
                    app.update_playback()
                    self.assertIsNone(app.playback_file)
                    self.assertEqual(max(app.playback_peaks), 0)
            summary_frame = app.summary.master
            transcript_frame = app.transcript.master
            panes = summary_frame.master.panes()
            self.assertEqual(str(panes[0]), str(summary_frame))
            self.assertEqual(str(panes[1]), str(transcript_frame))
        finally:
            app.destroy()

    def test_calendar_selects_date(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.date.set('2026-10-25')
            app.show_calendar()
            window = [w for w in app.winfo_children() if w.winfo_class() == 'Toplevel'][0]
            grid = window.winfo_children()[1]
            buttons = [w for w in grid.winfo_children() if w.winfo_class() == 'TButton']
            first = next(w for w in buttons if str(w.cget('text')) == '1')
            first.invoke()
            self.assertEqual(app.date.get(), '2026-10-01')
            self.assertFalse(window.winfo_exists())
        finally:
            app.destroy()

    def test_time_and_storage(self):
        self.assertEqual(timestamp(3661.9), '01:01:01')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'record.json'
            atomic_json(path, {'title': '中文'})
            self.assertIn('中文', path.read_text(encoding='utf-8'))

    def test_summary_validates_output(self):
        config = dict(key='mock', model='test', prompt='中文要点')
        with patch('core.api', return_value={'choices': [{'message': {'content': '会议要点'}}]}):
            self.assertEqual(summarize(config, '测试原文'), '会议要点')
        with patch('core.api', return_value={'choices': [{'message': {'content': ''}}]}):
            with self.assertRaises(ValueError):
                summarize(config, '测试原文')

    def test_deepseek_probe_disables_thinking_and_skips_summary_prompt(self):
        config = dict(base_url='https://api.deepseek.com/v1', key='mock', model='deepseek-flash', prompt='纪要专用要求')
        with patch('core.api', return_value={'choices': [{'finish_reason': 'stop', 'message': {'content': '连接成功'}}]}) as request:
            self.assertEqual(summarize(config, '', True), '连接成功')
            payload = request.call_args.args[2]
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertGreater(payload['max_tokens'], 32)
            self.assertEqual(len(payload['messages']), 1)
            self.assertNotIn('纪要专用要求', str(payload['messages']))
        config['base_url'] = 'https://other.example/v1'
        with patch('core.api', return_value={'choices': [{'message': {'content': '纪要'}}]}) as request:
            summarize(config, '原文')
            self.assertNotIn('thinking', request.call_args.args[2])

    def test_reasoning_truncation_retries_without_using_reasoning_as_summary(self):
        config = dict(key='mock', model='reasoner', prompt='中文要点')
        responses = [
            {'choices': [{'finish_reason': 'length', 'message': {'content': '', 'reasoning_content': '思考过程'}}]},
            {'choices': [{'finish_reason': 'stop', 'message': {'content': '最终纪要', 'reasoning_content': '思考过程'}}]},
        ]
        budgets = []
        def respond(config, endpoint, payload):
            budgets.append(payload['max_tokens'])
            return responses.pop(0)
        with patch('core.api', side_effect=respond):
            self.assertEqual(summarize(config, '原文'), '最终纪要')
        self.assertEqual(budgets, [8192, 16384])

    def test_truncated_or_malformed_summary_is_rejected(self):
        config = dict(key='mock', model='reasoner', prompt='中文要点')
        cases = [
            ({'choices': []}, 'choices'),
            ({'choices': [{'finish_reason': 'length', 'message': {'content': '未完成的纪要'}}]}, '长度上限'),
            ({'choices': [{'finish_reason': 'stop', 'message': {'content': '', 'reasoning_content': '思考'}}]}, '仅返回思考过程'),
            ({'choices': [{'finish_reason': 'content_filter', 'message': {}}]}, '内容过滤器'),
        ]
        for result, error in cases:
            with self.subTest(error=error), patch('core.api', return_value=result):
                with self.assertRaisesRegex(ValueError, error):
                    summarize(config, '原文')

    def test_text_part_response_is_supported(self):
        config = dict(key='mock', model='test', prompt='中文要点')
        result = {'choices': [{'message': {'content': [{'type': 'text', 'text': '主题'}, {'type': 'text', 'text': '待办'}]}}]}
        with patch('core.api', return_value=result):
            self.assertEqual(summarize(config, '原文'), '主题\n待办')

    def test_ui_library_and_validation(self):
        with patch('app.read_index', return_value=[]):
            app = App()
        try:
            app.update()
            self.assertEqual(len(app.tabs.tabs()), 3)
            self.assertEqual(app.verified, bool(load_verification(app.config_data)))
            with tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                (folder / 'transcript.txt').write_text('原文测试', encoding='utf-8')
                (folder / 'summary.md').write_text('纪要测试', encoding='utf-8')
                app.index = [dict(id=str(i), title=f'测试{i}', date='2026-10-25',
                                  created=f'2026-10-25T10:00:0{i}', duration=61,
                                  folder=temp, state='完成') for i in range(2)]
                app.date.set('2026-10-25')
                app.refresh_library()
                self.assertEqual(app.selected['id'], '0')
                self.assertEqual(len(app.records['values']), 2)
                self.assertIn('原文测试', app.transcript.get('1.0', 'end'))
                self.assertIn('纪要测试', app.summary.get('1.0', 'end'))
                app.records.current(1)
                app.select_record()
                self.assertEqual(app.selected['id'], '1')
                app.style_label.set('自定义')
                app.change_summary_style()
                app.prompt.insert('end', '字' * 1001)
                with self.assertRaises(ValueError):
                    app.settings_snapshot()
        finally:
            app.destroy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
