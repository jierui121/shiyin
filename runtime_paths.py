"""区分源码与打包后的程序、用户数据目录。"""
import os
import sys
from pathlib import Path


def runtime_paths():
    if getattr(sys, 'frozen', False):
        root = Path(sys.executable).resolve().parent
        data = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'Shiyin' / 'data'
    else:
        root = Path(__file__).resolve().parent
        data = root / 'data'
    return root, data
