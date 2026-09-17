"""检索源公共类型与 HTTP 工具。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import activity
from ..config import settings

# 可信度等级（数字越小越可信）：与内部技术路线 3.7 一致
CREDIBILITY = {
    "experiment": 1,   # 自己的实验数据
    "tds": 2,          # 供应商 TDS / 官方产品页
    "journal": 3,      # 同行评审期刊
    "patent": 4,       # 专利
    "industry": 5,     # 行业网站 / 价格行情
    "forum": 6,        # 论坛 / 公众号
}


@dataclass
class SearchHit:
    provider: str
    external_id: str
    title: str
    source_type: str = "paper"        # paper | patent | web | tds | price
    authors: str = ""
    year: int | None = None
    venue: str = ""
    doi: str = ""
    url: str = ""
    abstract: str = ""
    oa_pdf_url: str = ""
    credibility: int = 3
    license: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        if self.doi:
            return "doi:" + normalize_doi(self.doi)
        return "title:" + re.sub(r"\W+", "", self.title.lower())[:80]


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.lower()


def get_json(url: str, params: dict | None = None, headers: dict | None = None, timeout: float | None = None,
             retries: int = 2) -> dict[str, Any] | list | None:
    hdrs = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    with activity.track("search", _host(url)):
        return _get_json(url, params, hdrs, timeout, retries)


def _host(url: str) -> str:
    return urlparse(url).netloc


def _get_json(url: str, params: dict | None, hdrs: dict, timeout: float | None, retries: int) -> dict[str, Any] | list | None:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout or settings.http_timeout, follow_redirects=True) as c:
                r = c.get(url, params=params, headers=hdrs)
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                last = RuntimeError(f"HTTP {r.status_code} {url}: {r.text[:200]}")
                if r.status_code in (401, 403, 404):
                    raise last
                time.sleep(1.0)
                continue
            return r.json()
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    if last:
        raise last
    return None


def get_text(url: str, params: dict | None = None, headers: dict | None = None, timeout: float | None = None) -> tuple[str, str]:
    """返回 (content_type, text)。"""
    hdrs = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
    if headers:
        hdrs.update(headers)
    with activity.track("search", _host(url)), httpx.Client(timeout=timeout or settings.http_timeout, follow_redirects=True) as c:
        r = c.get(url, params=params, headers=hdrs)
        r.raise_for_status()
        return r.headers.get("content-type", ""), r.text


def get_bytes(url: str, headers: dict | None = None, timeout: float | None = None) -> tuple[str, bytes]:
    hdrs = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
    if headers:
        hdrs.update(headers)
    with activity.track("search", _host(url)), httpx.Client(timeout=timeout or 90.0, follow_redirects=True) as c:
        r = c.get(url, headers=hdrs)
        r.raise_for_status()
        return r.headers.get("content-type", ""), r.content


def dedupe(hits: list[SearchHit]) -> list[SearchHit]:
    seen: dict[str, SearchHit] = {}
    for h in hits:
        k = h.key
        if k in seen:
            # 合并：补齐缺失字段（摘要、OA 链接）
            cur = seen[k]
            if not cur.abstract and h.abstract:
                cur.abstract = h.abstract
            if not cur.oa_pdf_url and h.oa_pdf_url:
                cur.oa_pdf_url = h.oa_pdf_url
            if not cur.doi and h.doi:
                cur.doi = h.doi
            cur.meta.setdefault("also_from", []).append(h.provider)
        else:
            seen[k] = h
    return list(seen.values())
