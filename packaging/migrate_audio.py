"""仅迁移笔记索引中的历史 WAV；完整解码并验证时长后删除 WAV。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_media import encode_mp3
import av


def migrate():
    root = Path(__file__).resolve().parent.parent
    indices = [root / 'data' / 'library.json', Path(os.environ['LOCALAPPDATA']) / 'Shiyin' / 'data' / 'library.json']
    folders = set()
    for index in indices:
        if index.is_file():
            folders.update(Path(record['folder']) for record in json.loads(index.read_text(encoding='utf-8')))
    converted = 0
    for number, folder in enumerate(sorted(folders), 1):
        source, target = folder / 'audio.wav', folder / 'audio.mp3'
        if not source.is_file():
            continue
        if target.exists():
            print(f'跳过已有 MP3，保留 WAV：{folder}', flush=True)
            continue
        print(f'[{number}/{len(folders)}] 转换：{folder.name}', flush=True)
        duration = encode_mp3(source, target)
        with av.open(str(target)) as audio:
            decoded = sum(frame.samples / frame.sample_rate for frame in audio.decode(audio=0))
        if abs(decoded - duration) > 0.15:
            raise ValueError(f'转换时长校验失败，保留 WAV：{folder}')
        source.unlink()
        converted += 1
        print(f'校验通过：{duration:.2f} 秒，已改为 MP3', flush=True)
    print(f'迁移完成：{converted} 条', flush=True)


if __name__ == '__main__':
    migrate()
