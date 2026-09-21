"""网页 / PDF 抓取：URL → 纯文本（正文提取）或下载到 data/downloads。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..config import DOWNLOAD_DIR
from .base import get_bytes


def _safe_name(url: str, ext: str) -> Path:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", url.rsplit("/", 1)[-1])[:40] or "file"
    return DOWNLOAD_DIR / f"{stem}_{h}{ext}"


def fetch_url(url: str) -> dict:
    """返回 {kind: 'pdf'|'html'|'text', text, file_path, title}。"""
    ctype, content = get_bytes(url)
    ctype = (ctype or "").lower()
    is_pdf = "pdf" in ctype or content[:5] == b"%PDF-" or url.lower().endswith(".pdf")
    if is_pdf:
        path = _safe_name(url, ".pdf")
        path.write_bytes(content)
        from ..ingest.parsers import parse_pdf

        text, meta = parse_pdf(path)
        return {"kind": "pdf", "text": text, "file_path": str(path), "title": meta.get("title", "")}
    html = content.decode("utf-8", errors="ignore")
    try:
        import trafilatura

        text = trafilatura.extract(html, include_tables=True, include_comments=False, favor_recall=True) or ""
        md = trafilatura.extract_metadata(html)
        title = (md.title if md else "") or ""
        page_date = (md.date if md else "") or ""
    except Exception:  # noqa: BLE001
        text, title, page_date = "", "", ""
    if not text:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
        title = title or (soup.title.string.strip() if soup.title and soup.title.string else "")
    return {"kind": "html", "text": text.strip(), "file_path": "", "title": title, "date": page_date}
