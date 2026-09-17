"""清空运行产物（数据库、状态、日志、知识库自动生成文件），保留代码与 .env。

用法：.venv\\Scripts\\python.exe scripts\\reset_project.py --yes
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polysage.config import DATA_DIR, KB  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="确认删除")
    ap.add_argument("--keep-downloads", action="store_true")
    a = ap.parse_args()
    if not a.yes:
        print("将删除 data/ 下的数据库、状态、日志与 knowledge/ 下自动生成的卡片和报告。加 --yes 确认。")
        return
    for p in DATA_DIR.glob("polysage.db*"):
        p.unlink()
    for name in ("pipeline_state.json", "pipeline.log"):
        (DATA_DIR / name).unlink(missing_ok=True)
    if not a.keep_downloads:
        for sub in ("downloads", "uploads", "index"):
            shutil.rmtree(DATA_DIR / sub, ignore_errors=True)
            (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    for key, d in KB.items():
        for p in d.iterdir():
            if p.is_file() and p.name not in ("task.yaml", "constraints.yaml", "base.md", "过关规则.md", "检索关键词.yaml"):
                p.unlink()
    print("已清空。")


if __name__ == "__main__":
    main()
