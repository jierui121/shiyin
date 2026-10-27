from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

root = Path(SPECPATH).parent
datas = [(str(root / 'assets' / 'shiyin.ico'), 'assets')]
binaries = []
hiddenimports = []
for package in ('faster_whisper', 'ctranslate2', 'opencc', 'tokenizers'):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
datas += collect_data_files('_sounddevice_data')

a = Analysis([str(root / 'launcher.py')], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports,
             excludes=['pytest', 'IPython', 'matplotlib'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Shiyin', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'shiyin.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Shiyin')
