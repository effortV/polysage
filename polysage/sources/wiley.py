"""Wiley TDM 全文下载（https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining）。

- 请求头带 Wiley-TDM-Client-Token，按 DOI 取 PDF；只对 Wiley 的 DOI（10.1002/、10.1111/ 等）试。
- 开放获取的文章任何有效 token 都能下；订阅文章要求机构订阅，而且请求得从机构 IP 发出。
- 限速：每秒 3 次、每 10 分钟 60 次。这里每次下载前至少间隔 0.4 秒。
"""
from __future__ import annotations

import threading
import time
from urllib.parse import quote

from .. import activity, db, net
from ..config import DOWNLOAD_DIR, settings
from .base import normalize_doi

API = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/"
PREFIXES = ("10.1002/", "10.1111/", "10.1046/", "10.1029/", "10.1113/", "10.15252/")
_lock = threading.Lock()
_last = 0.0


def is_wiley(doi: str) -> bool:
    return normalize_doi(doi or "").lower().startswith(PREFIXES)


def _download(doi: str, token: str):
    global _last
    with _lock:
        wait = 0.4 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
    url = API + quote(normalize_doi(doi), safe="")
    with activity.track("search", "api.wiley.com"):
        with net.client(url, timeout=90.0, follow_redirects=True) as c:
            return c.get(url, headers={"Wiley-TDM-Client-Token": token, "User-Agent": settings.user_agent})


def get_fulltext(doi: str) -> tuple[str, str] | None:
    """返回 (正文, 本地 PDF 路径)；没 token / 不是 Wiley 的 / 没权限都返回 None。"""
    token = settings.wiley_tdm_token
    if not token or not is_wiley(doi):
        return None
    try:
        r = _download(doi, token)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200 or r.content[:5] != b"%PDF-":
        return None
    path = DOWNLOAD_DIR / ("wiley_" + normalize_doi(doi).replace("/", "_").replace(":", "_")[:80] + ".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    from ..ingest.parsers import parse_pdf

    text, _meta = parse_pdf(path)
    return (text, str(path)) if text and len(text) > 1500 else None


def _sample_oa_doi() -> str:
    """找一篇 Wiley 开放获取文章测 token：Advanced Science 整刊开放获取；查不到就用知识库里的 Wiley DOI。"""
    try:
        from .base import get_json

        data = get_json("https://api.crossref.org/journals/2198-3844/works",
                        params={"rows": 1, "select": "DOI", "sort": "published", "order": "desc"}, timeout=20, retries=1)
        items = ((data or {}).get("message") or {}).get("items") or []
        if items and items[0].get("DOI"):
            return items[0]["DOI"]
    except Exception:  # noqa: BLE001
        pass
    row = db.q1("SELECT doi FROM sources WHERE doi LIKE '10.1002/%' ORDER BY id DESC LIMIT 1")
    return (row or {}).get("doi") or ""


def ping(token: str | None = None) -> tuple[bool, str]:
    token = token or settings.wiley_tdm_token
    if not token:
        return False, "没填 Wiley TDM token。"
    doi = _sample_oa_doi()
    if not doi:
        return False, "找不到用来测试的 Wiley 文章（Crossref 连不上，知识库里也没有 Wiley 的 DOI）。"
    try:
        r = _download(doi, token)
    except Exception as e:  # noqa: BLE001
        return False, f"连不上 api.wiley.com：{e}"
    if r.status_code == 200 and r.content[:5] == b"%PDF-":
        return True, f"可用：下到了 {doi} 的 PDF（{len(r.content) // 1024} KB）。订阅文章还要看服务器 IP 是否在机构订阅范围内。"
    if r.status_code in (401, 403):
        return False, f"{r.status_code}：token 无效，或这篇没有权限（测的是开放获取文章 {doi}，多半是 token 不对）。"
    if r.status_code == 429:
        return False, "429：请求太频繁，过几分钟再测。"
    return False, f"异常 {r.status_code}：{r.text[:200]}"
