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
    page_date = _better_date(url, html, title, page_date)
    return {"kind": "html", "text": text.strip(), "file_path": "", "title": title, "date": page_date}


_TITLE_DATE = re.compile(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})")


def _better_date(url: str, html: str, title: str, page_date: str) -> str:
    """trafilatura 的日期常被微信/门户页面里的旧日期骗过：微信文章用 ct 时间戳；标题里写明的日期（2026.09.15）优先。"""
    m = _TITLE_DATE.search(title or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m2 = re.search(r"(?<![\d.])(\d{1,2})[.\-月](\d{1,2})(?:日|-|\)|）| )", title or "")  # 周评(9.14-9.18)：没写年份的按今年
    if m2:
        from datetime import date, timedelta

        try:
            d = date(date.today().year, int(m2.group(1)), int(m2.group(2)))
            if timedelta(0) <= date.today() - d <= timedelta(days=60):
                return d.isoformat()
        except ValueError:
            pass
    if "mp.weixin.qq.com" in url:
        mc = re.search(r'var\s+ct\s*=\s*"(\d{10})"', html) or re.search(r'"publish_time"\s*:\s*"?(\d{10})', html)
        if mc:
            from datetime import datetime

            return datetime.fromtimestamp(int(mc.group(1))).strftime("%Y-%m-%d")
    mp = re.search(r'property="article:published_time"\s+content="(20\d{2}-\d{2}-\d{2})', html)
    if mp:
        return mp.group(1)
    return page_date
