"""把对话回答（Markdown）转成 Word，并从回答里认出它生成的文件，供界面直接下载。"""
from __future__ import annotations

import re
from pathlib import Path

from .config import DATA_DIR, KB, KB_DIR

EXPORT_DIR = DATA_DIR / "exports"
FILE_EXTS = (".xlsx", ".xls", ".csv", ".docx", ".md", ".json", ".pdf")
_PATH_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff_\-./\\]+\.(?:xlsx|xls|csv|docx|md|json|pdf)")


def referenced_files(text: str, limit: int = 8) -> list[Path]:
    """从回答正文里找出它提到的产出文件（如 `05_配方库/智能体方案_2026-09-24T0321.xlsx`），只返回真实存在的。"""
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in _PATH_RE.findall(text or ""):
        cand = raw.strip("`'\"（）()，,。;；").replace("\\", "/")
        for p in _candidates(cand):
            try:
                if p.is_file() and p not in seen:
                    seen.add(p)
                    out.append(p)
                    break
            except OSError:
                continue
        if len(out) >= limit:
            break
    return out


def _candidates(rel: str) -> list[Path]:
    p = Path(rel)
    cands = [p] if p.is_absolute() else []
    cands.append(KB_DIR / rel)                      # knowledge/05_配方库/xxx.xlsx
    cands.append(DATA_DIR.parent / rel)             # 项目根下的相对路径
    name = p.name
    for d in KB.values():                           # 只给了文件名时，在各知识库目录里找
        cands.append(d / name)
    return cands


# ---------------- Markdown → Word ----------------

def answer_to_docx(text: str, path: Path | None = None, title: str = "包装膜配方助手 · 回答") -> Path:
    """把回答转成 .docx（标题、列表、表格、加粗、中文字体），排版和试验方案、运行报告一致。"""
    from . import wordout as W

    path = path or EXPORT_DIR / f"回答_{_stamp()}.docx"
    doc = W.new_doc(title)
    W.add_markdown(doc, text or "")
    return W.save(doc, path)


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")
