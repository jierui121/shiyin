"""模型下载与离线定位；不改变全局网络配置。"""
import hashlib
import logging
from pathlib import Path
import requests

MODEL_FILES = ('config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt')
ENDPOINTS = ('https://huggingface.co', 'https://hf-mirror.com')


def complete(folder):
    return all((folder / name).is_file() and (folder / name).stat().st_size > 0 for name in MODEL_FILES)


def ensure_model(name, bundled_root, cache_root, report=lambda message: None):
    from core import ASR_MODELS
    if name not in ASR_MODELS:
        raise ValueError('请从列表选择转写模型。')
    for base in dict.fromkeys((Path(bundled_root), Path(cache_root))):
        if complete(base / name):
            report(f'{name} 模型已在本地，正在加载…')
            return base / name
        snapshots = base / f'models--Systran--faster-whisper-{name}' / 'snapshots'
        if snapshots.exists():
            for snapshot in sorted(snapshots.iterdir(), reverse=True):
                if complete(snapshot):
                    return snapshot
    repo = f'Systran/faster-whisper-{name}'
    metadata = None
    active = None
    for endpoint in ENDPOINTS:
        report(f'{name} 尚未下载，正在连接' + ('官方模型站点…' if endpoint == ENDPOINTS[0] else '备用下载站点…'))
        try:
            response = requests.get(f'{endpoint}/api/models/{repo}?blobs=true', timeout=(8, 15))
            response.raise_for_status()
            metadata = response.json()
            if not isinstance(metadata.get('sha'), str) or not isinstance(metadata.get('siblings'), list):
                raise ValueError('模型站点返回无效元数据')
            active = endpoint
            break
        except (requests.RequestException, ValueError):
            metadata = None
            logging.exception('模型元数据获取失败 %s %s', name, endpoint)
    if metadata is None:
        raise ValueError(f'{name} 模型首次下载失败：官方和备用下载站点均无法连接。音频已保存，可恢复网络后重试。')
    entries = {entry['rfilename']: entry for entry in metadata['siblings']}
    folder = Path(cache_root) / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for filename in MODEL_FILES:
            info = entries[filename]
            expected_size = int(info['size'])
            target = folder / filename
            if target.exists() and target.stat().st_size == expected_size:
                continue
            partial = target.with_suffix(target.suffix + '.part')
            endpoints = [active] + [endpoint for endpoint in ENDPOINTS if endpoint != active]
            last_error = None
            for endpoint in endpoints:
                try:
                    offset = partial.stat().st_size if partial.exists() else 0
                    headers = {'Range': f'bytes={offset}-'} if offset else {}
                    url = f"{endpoint}/{repo}/resolve/{metadata['sha']}/{filename}"
                    with requests.get(url, headers=headers, stream=True, timeout=(8, 45)) as response:
                        response.raise_for_status()
                        if response.status_code != 206:
                            offset = 0
                        downloaded = offset
                        previous_percent = -1
                        with partial.open('ab' if offset else 'wb') as stream:
                            for block in response.iter_content(1024 * 1024):
                                if not block:
                                    continue
                                stream.write(block)
                                downloaded += len(block)
                                percent = int(downloaded * 100 / expected_size)
                                if percent != previous_percent:
                                    report(f'正在下载 {name} / {filename} · {percent}%（{downloaded / 1024**2:.0f}/{expected_size / 1024**2:.0f} MiB）')
                                    previous_percent = percent
                    if partial.stat().st_size != expected_size:
                        raise ValueError('模型下载长度不匹配')
                    report(f'正在校验 {name} / {filename}…')
                    if info.get('lfs', {}).get('sha256'):
                        with partial.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                        expected = info['lfs']['sha256']
                    else:
                        digest = hashlib.sha1(f'blob {expected_size}\0'.encode() + partial.read_bytes()).hexdigest()
                        expected = info['blobId']
                    if digest != expected:
                        raise ValueError('模型文件校验失败，请删除对应 .part 文件后重试')
                    partial.replace(target)
                    last_error = None
                    break
                except (requests.RequestException, ValueError) as exc:
                    last_error = exc
                    logging.exception('模型文件下载失败 %s %s', name, filename)
            if last_error:
                raise last_error
    except (OSError, requests.RequestException, ValueError, KeyError) as exc:
        raise ValueError(f'{name} 模型尚未下载完整：{exc}。音频已保存，已有下载进度会保留。') from exc
    report(f'{name} 下载与校验完成，正在加载本地模型…')
    return folder
