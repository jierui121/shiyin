# 开发指南

拾音使用 Python、Tkinter 和 faster-whisper，面向 Windows 桌面运行。源码版的数据位于项目的 `data/`，发行版的数据位于 `%LOCALAPPDATA%\Shiyin\data`。

## 环境与启动

在项目目录内使用虚拟环境。已有 `.venv` 时可直接使用；新安装可按以下步骤准备：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.bat
```

开发环境使用 Python 3.13。Tkinter 需要随 Python 一起安装。运行依赖见 `requirements.txt`，发行构建的依赖快照见 `requirements-lock.txt`。

`start.bat` 使用 UTF-8 无 BOM、Windows CRLF 换行；编辑时保留这一格式。

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `app.py` | 桌面界面、录音流程、笔记库与播放交互 |
| `core.py` | 笔记存储、Windows DPAPI 与纪要 API |
| `audio_import.py` / `audio_media.py` | 音频导入、MP3 转换、波形读取与播放 |
| `model_store.py` | 模型定位、下载、断点续传与完整性校验 |
| `runtime_paths.py` | 源码版与发行版的路径处理 |
| `launcher.py` | 启动入口与错误提示 |
| `test_app.py` | 离线回归测试 |
| `packaging/` / `build.ps1` | Windows 打包与发行包组装 |

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest test_app -q
```

测试覆盖录音与保存流程、音频导入和转换、播放定位、笔记编辑、日期管理、删除与失败还原、API 返回处理等。测试使用临时目录及模拟对象，不使用真实麦克风，不发送真实纪要请求。

真实麦克风表现、目标电脑上的运行情况及具体 API 服务商的可用性，需要在对应环境中检查。

## Windows 打包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

打包脚本会将 `dist/Shiyin` 中的程序与本地 small 模型组装到发行目录，再生成 `release/Shiyin-Windows-x64.zip`。运行前需准备好 `models/models--Systran--faster-whisper-small/snapshots/` 下的模型，可通过源码版选择 small 并完成首次转写下载。

发布新版本时，同步调整 `packaging/release_bundle.py` 中的版本目录，并核对：

- 发行 ZIP 可完整解压，EXE、`_internal` 和 `models` 齐全。
- 包内不包含个人设置、API Key、录音、原文、纪要或运行日志。
- 发行版能够从内置 small 模型离线转写，数据写入应用目录之外。
- 上传到 GitHub Releases 的附件大小及 SHA256 与本地文件一致。

安装包通过 GitHub Releases 分发；源码仓库不提交模型、虚拟环境和构建输出。

## 实现约定

默认使用 CPU int8 转写。纪要接口兼容 OpenAI 的 `chat/completions` 格式，API Key 使用 Windows DPAPI 保护。

个人数据、模型、日志及本机验证记录由 `.gitignore` 排除。调试和提交时避免将这些内容加入版本控制。
