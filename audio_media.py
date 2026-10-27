"""MP3 编码、流式波形读取与 Windows 原生可定位播放。"""
import ctypes
import uuid
from pathlib import Path

import av


def audio_path(folder):
    folder = Path(folder)
    mp3 = folder / 'audio.mp3'
    return mp3 if mp3.is_file() else folder / 'audio.wav'


def encode_mp3(source, target, notify=lambda message: None):
    target = Path(target)
    temporary = target.with_suffix('.part.mp3')
    samples = 0
    reported = -1
    try:
        with av.open(str(source)) as input_audio, av.open(str(temporary), 'w', format='mp3') as output:
            if not input_audio.streams.audio:
                raise ValueError('文件中没有音轨。')
            stream = output.add_stream('libmp3lame', rate=16000)
            stream.layout = 'mono'
            stream.bit_rate = 64000
            resampler = av.AudioResampler(format='fltp', layout='mono', rate=16000)

            def write(frames):
                nonlocal samples, reported
                for frame in frames:
                    frame.pts = None
                    samples += frame.samples
                    for packet in stream.encode(frame):
                        output.mux(packet)
                seconds = samples // 16000
                if seconds != reported:
                    notify(f'正在转换 MP3 · 已处理 {seconds} 秒')
                    reported = seconds

            for frame in input_audio.decode(audio=0):
                frame.pts = None
                write(resampler.resample(frame))
            write(resampler.resample(None))
            for packet in stream.encode(None):
                output.mux(packet)
        if not samples:
            raise ValueError('录音为空或无法解码。')
        temporary.replace(target)
        return samples / 16000
    finally:
        temporary.unlink(missing_ok=True)


class PreviewAudio:
    """按需解码波形，不把整段长录音加载到内存。"""
    def __init__(self, path):
        self.container = av.open(str(path))
        self.stream = self.container.streams.audio[0]
        self.duration = float(self.stream.duration * self.stream.time_base) if self.stream.duration else float(self.container.duration or 0) / av.time_base
        self.position = 0
        self.setpos(0)

    def getframerate(self):
        return 16000

    def getnframes(self):
        return int(self.duration * 16000)

    def tell(self):
        return self.position

    def setpos(self, position):
        self.position = position
        self.container.seek(int(position / 16000 / self.stream.time_base), stream=self.stream, backward=True)
        self.frames = self.container.decode(audio=0)
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        self.buffer = bytearray()
        self.skip_until = position
        self.decoded_position = 0

    def readframes(self, count):
        while len(self.buffer) < count * 2:
            try:
                frame = next(self.frames)
            except StopIteration:
                break
            start = round(frame.time * 16000) if frame.time is not None else self.decoded_position
            frame.pts = None
            for converted in self.resampler.resample(frame):
                raw = converted.to_ndarray().tobytes()
                skip = max(0, self.skip_until - start)
                self.buffer.extend(raw[min(skip, converted.samples) * 2:])
                start += converted.samples
            self.decoded_position = start
        raw = bytes(self.buffer[:count * 2])
        del self.buffer[:len(raw)]
        self.position += len(raw) // 2
        return raw

    def close(self):
        self.container.close()


class NativePlayer:
    def __init__(self, path):
        self.alias = 'shiyin_' + uuid.uuid4().hex
        self.command(f'open "{path}" type mpegvideo alias {self.alias}')

    @staticmethod
    def command(command):
        result = ctypes.create_unicode_buffer(512)
        error = ctypes.windll.winmm.mciSendStringW(command, result, len(result), None)
        if error:
            ctypes.windll.winmm.mciGetErrorStringW(error, result, len(result))
            raise OSError(result.value)
        return result.value

    def play(self, seconds=0):
        self.command(f'play {self.alias} from {int(seconds * 1000)}')

    def position(self):
        return int(self.command(f'status {self.alias} position')) / 1000

    def close(self):
        self.command(f'close {self.alias}')
