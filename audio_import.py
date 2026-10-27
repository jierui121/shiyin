"""使用已有 PyAV 依赖，将外部录音流式转换为 MP3。"""
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from audio_media import encode_mp3


def import_audio(source, storage, date, use_ai=False, notify=lambda message: None):
    source = Path(source)
    datetime.strptime(date, '%Y-%m-%d')
    if not source.is_file():
        raise ValueError('所选录音文件不存在。')
    storage = Path(storage)
    storage.mkdir(parents=True, exist_ok=True)
    base = storage / source.stem
    folder = base
    number = 2
    while True:
        try:
            folder.mkdir()
            break
        except FileExistsError:
            folder = base.with_name(base.name + f'_{number}')
            number += 1
    try:
        duration = encode_mp3(source, folder / 'audio.mp3', notify)
        (folder / 'transcript.txt').write_text('等待本地转写…', encoding='utf-8')
        (folder / 'summary.md').write_text('等待生成纪要…' if use_ai else '本次录音未开启 AI 纪要。', encoding='utf-8')
        return dict(id=uuid.uuid4().hex, title=folder.name, date=date,
                    created=datetime.now().isoformat(timespec='seconds'), duration=duration,
                    folder=str(folder), state='等待转写', source='import', original_name=source.name)
    except Exception:
        # 只清理由本次导入新建的目录，保留源文件和既有笔记。
        shutil.rmtree(folder)
        raise
