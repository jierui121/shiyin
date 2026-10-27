"""用标准库绘制应用图标，无额外绘图依赖。"""
import struct
import zlib
from pathlib import Path


def make_icon():
    size = 64
    pixels = bytearray(size * size * 4)
    heights = [12, 22, 34, 46, 32, 22, 14]
    for y in range(size):
        for x in range(size):
            distance = max(0, 12-x, x-51)**2 + max(0, 12-y, y-51)**2
            color = (56, 105, 219, 255) if distance <= 144 else (0, 0, 0, 0)
            for i, height in enumerate(heights):
                if 10+i*7 <= x < 14+i*7 and abs(y-32) <= height/2:
                    color = (255, 255, 255, 255)
            offset = (y*size+x)*4
            pixels[offset:offset+4] = bytes(color)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    scan = b''.join(b'\0'+pixels[y*size*4:(y+1)*size*4] for y in range(size))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB',size,size,8,6,0,0,0)) + chunk(b'IDAT',zlib.compress(scan)) + chunk(b'IEND',b'')
    output = Path(__file__).resolve().parent.parent / 'assets' / 'shiyin.ico'
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(struct.pack('<HHH',0,1,1) + struct.pack('<BBBBHHII',size,size,0,0,1,32,len(png),22) + png)


if __name__ == '__main__':
    make_icon()
