"""桌面启动入口：将启动异常写入日志并显示中文提示。"""
import ctypes
import sys
import traceback
from datetime import datetime
from pathlib import Path


def main():
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == '--self-test':
            from packaged_check import run_check
            return run_check(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else None)
        from app import App
        App().mainloop()
    except Exception:
        detail = traceback.format_exc()
        from runtime_paths import runtime_paths
        log = runtime_paths()[1] / 'startup.log'
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('a', encoding='utf-8') as stream:
                stream.write(f'\n{datetime.now().isoformat(timespec="seconds")}\n{detail}')
            message = f'软件启动失败，错误记录已保存到：\n{log}\n\n{detail.splitlines()[-1]}'
        except OSError:
            message = f'软件启动失败：\n{detail}'
        ctypes.windll.user32.MessageBoxW(None, message, '拾音 · 启动失败', 0x10)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
