"""读写项目根目录的 .env（设置页、改密码用）；写完立刻重新加载设置，不用重启服务。"""
from __future__ import annotations

import os
import re

from .config import ROOT, reload_settings

ENV_PATH = ROOT / ".env"


def set_env(pairs: dict[str, str | None], remove: list[str] | None = None) -> None:
    """改/加若干行；值为 None 的跳过（“留空不改”）；remove 里的键整行删掉。"""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    for key in remove or []:
        text = re.sub(rf"(?m)^{re.escape(key)}=.*(?:\n|$)", "", text)
        os.environ.pop(key, None)
    for key, value in pairs.items():
        if value is None:
            continue
        line = f"{key}={str(value).replace(chr(10), ' ').strip()}"
        if re.search(rf"(?m)^{re.escape(key)}=.*$", text):
            text = re.sub(rf"(?m)^{re.escape(key)}=.*$", lambda _m, ln=line: ln, text)   # 用函数替换，值里的反斜杠不转义
        else:
            text += ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    reload_settings(ENV_PATH)
