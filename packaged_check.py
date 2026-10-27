"""发行包自检入口，不录制环境音、不请求远端 API。"""
import json
import traceback
from pathlib import Path


def run_check(report_path, audio_path=None):
    result = {}
    try:
        from app import App
        from core import ROOT, DATA, simplified
        from faster_whisper import WhisperModel
        import sounddevice
        result['simplified'] = simplified('會議紀錄') == '会议纪录'
        result['audio_devices'] = len(sounddevice.query_devices())
        app = App()
        app.update()
        result['tabs'] = len(app.tabs.tabs())
        app.destroy()
        result['data_outside_bundle'] = DATA.parent != ROOT
        model_path = ROOT / 'models' / 'small'
        model = WhisperModel(str(model_path), device='cpu', compute_type='int8', local_files_only=True)
        result['offline_model_loaded'] = True
        if audio_path:
            import tempfile
            from audio_media import encode_mp3, PreviewAudio, NativePlayer
            with tempfile.TemporaryDirectory() as temp:
                mp3 = Path(temp) / 'audio.mp3'
                encode_mp3(audio_path, mp3)
                preview = PreviewAudio(mp3)
                preview.setpos(16000)
                result['mp3_seek_decode'] = bool(preview.readframes(1600))
                preview.close()
                player = NativePlayer(mp3)
                player.close()
                result['native_mp3_open'] = True
                segments, _ = model.transcribe(str(mp3), vad_filter=True)
                text = ' '.join(segment.text for segment in segments)
                result['transcript'] = text
                assert 'meeting' in text.lower(), text
                assert result['mp3_seek_decode']
        assert result['tabs'] == 3 and result['simplified']
        result['success'] = True
    except Exception:
        result['success'] = False
        result['error'] = traceback.format_exc()
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['success'] else 1
