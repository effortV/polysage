"""文本切片：按段落 / 句子合并到目标长度，带重叠；中英文通用。"""
from __future__ import annotations

import re

SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+(?=[A-Z(\[])")


def split_sentences(text: str) -> list[str]:
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        for s in SENT_SPLIT.split(para):
            s = s.strip()
            if s:
                out.append(s)
        out.append("\n")  # 段落边界标记
    return out


def chunk_text(text: str, target: int = 700, max_len: int = 1000, overlap: int = 120) -> list[str]:
    """把文本切成 ~target 字符的块；块间保留 overlap 字符重叠。"""
    sents = split_sentences(text)
    chunks: list[str] = []
    buf = ""
    for s in sents:
        if s == "\n":
            if len(buf) >= target * 0.6:
                chunks.append(buf.strip())
                buf = buf[-overlap:] if overlap else ""
            else:
                buf += "\n"
            continue
        # 超长句子硬切
        while len(s) > max_len:
            piece, s = s[:max_len], s[max_len:]
            if buf.strip():
                chunks.append(buf.strip())
            chunks.append(piece)
            buf = piece[-overlap:] if overlap else ""
        if len(buf) + len(s) + 1 > max_len and buf.strip():
            chunks.append(buf.strip())
            buf = (buf[-overlap:] if overlap else "") + " " + s
        else:
            buf = (buf + " " + s) if buf else s
        if len(buf) >= target:
            chunks.append(buf.strip())
            buf = buf[-overlap:] if overlap else ""
    if buf.strip() and (not chunks or buf.strip() != chunks[-1][-len(buf.strip()):]):
        chunks.append(buf.strip())
    # 去掉纯重叠的极短尾块
    return [c for c in chunks if len(c) >= 40 or len(chunks) == 1]
