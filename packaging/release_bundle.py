"""组装不含用户数据的完整免安装包。"""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
release = root / 'release'
release.mkdir(exist_ok=True)
target = release / 'Shiyin-1.3'
print('复制应用运行文件…', flush=True)
shutil.copytree(root / 'dist' / 'Shiyin', target, dirs_exist_ok=True)
snapshot = next((root / 'models' / 'models--Systran--faster-whisper-small' / 'snapshots').iterdir())
print('复制内置 small 模型…', flush=True)
shutil.copytree(snapshot, target / 'models' / 'small', dirs_exist_ok=True)
(target / 'README.txt').write_text('''拾音 · 本地录音笔记（Windows x64 免安装版）

解压整个 Shiyin 文件夹后双击 Shiyin.exe，无需安装 Python。
请保留 _internal 与 models 文件夹，不能只复制单独的 EXE。
默认 small 模型已随包提供，可离线转写；选择其他模型首次需要下载。
启用 AI 纪要需要联网并配置自己的 API。

设置、验证状态和录音索引：%LOCALAPPDATA%\\Shiyin\\data
录音文件按设置的存储路径保存；首次使用默认保存在上述 data\\recordings。
更新时替换程序文件夹即可，不要删除用户数据目录。
本发行包不含任何个人 API Key、录音或笔记。

首次启动故障日志：%LOCALAPPDATA%\\Shiyin\\data\\startup.log
运行日志：同目录 app.log
''', encoding='utf-8')
shutil.copy2(root / 'README.md', target / 'USER_GUIDE.md')
manifest = []
for path in sorted(target.rglob('*')):
    if path.is_file():
        relative = path.relative_to(target).as_posix()
        assert relative.split('/')[0] not in ('data', '.venv'), relative
        digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
        manifest.append(dict(path=relative, size=path.stat().st_size, sha256=digest))
(release / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
archive = release / 'Shiyin-Windows-x64.zip'
print('压缩完整应用，含离线模型…', flush=True)
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=5) as z:
    for path in sorted(target.rglob('*')):
        if path.is_file():
            z.write(path, str(path.relative_to(release)))
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
print(f'完成：{archive}，{archive.stat().st_size / 1024**2:.1f} MiB', flush=True)
