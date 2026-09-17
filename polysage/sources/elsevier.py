"""Elsevier API：Scopus 检索、ScienceDirect 检索、摘要检索、全文检索。

说明：
- Scopus Search / ScienceDirect Search 用 API key 即可拿到题录与（部分）摘要。
- Article Retrieval（全文）只对开放获取文章或已授权机构（校园 IP / insttoken）生效；
  拿不到全文时返回 None，由用户手动上传 PDF。
"""
from __future__ import annotations

from typing import Any

import httpx

from ..config import settings
from .base import SearchHit, get_json, normalize_doi

SCOPUS = "https://api.elsevier.com/content/search/scopus"
SCIDIR = "https://api.elsevier.com/content/search/sciencedirect"
ABSTRACT = "https://api.elsevier.com/content/abstract/doi/"
ARTICLE = "https://api.elsevier.com/content/article/doi/"


def _headers() -> dict[str, str]:
    if not settings.elsevier_api_key:
        raise RuntimeError("未配置 ELSEVIER_API_KEY")
    h = {"X-ELS-APIKey": settings.elsevier_api_key, "Accept": "application/json"}
    if settings.elsevier_insttoken:
        h["X-ELS-Insttoken"] = settings.elsevier_insttoken
    return h


def _scopus_hit(e: dict[str, Any]) -> SearchHit:
    doi = normalize_doi(e.get("prism:doi") or "")
    url = ""
    for ln in e.get("link") or []:
        if ln.get("@ref") == "scopus":
            url = ln.get("@href") or ""
    year = None
    cd = e.get("prism:coverDate") or ""
    if cd[:4].isdigit():
        year = int(cd[:4])
    return SearchHit(
        provider="scopus",
        external_id=(e.get("dc:identifier") or "").replace("SCOPUS_ID:", ""),
        title=e.get("dc:title") or "",
        source_type="paper",
        authors=e.get("dc:creator") or "",
        year=year,
        venue=e.get("prism:publicationName") or "",
        doi=doi,
        url=(f"https://doi.org/{doi}" if doi else url),
        abstract=e.get("dc:description") or "",
        credibility=3,
        meta={"citedby": e.get("citedby-count"), "openaccess": e.get("openaccessFlag"), "scopus_url": url},
    )


def search_scopus(query: str, limit: int = 25, year_from: int | None = None) -> list[SearchHit]:
    q = f"TITLE-ABS-KEY({query})"
    if year_from:
        q += f" AND PUBYEAR > {year_from - 1}"
    params = {"query": q, "count": min(limit, 25), "view": "STANDARD", "sort": "-relevancy"}
    data = get_json(SCOPUS, params=params, headers=_headers()) or {}
    entries = (data.get("search-results") or {}).get("entry") or []
    return [_scopus_hit(e) for e in entries if e.get("dc:title")]


def _scidir_hit(r: dict[str, Any]) -> SearchHit:
    doi = normalize_doi(r.get("doi") or "")
    authors = ", ".join(a.get("name", "") for a in (r.get("authors") or [])[:6])
    pd = r.get("publicationDate") or ""
    return SearchHit(
        provider="sciencedirect",
        external_id=r.get("pii") or doi,
        title=r.get("title") or "",
        source_type="paper",
        authors=authors,
        year=int(pd[:4]) if pd[:4].isdigit() else None,
        venue=r.get("sourceTitle") or "",
        doi=doi,
        url=(f"https://doi.org/{doi}" if doi else (r.get("uri") or "")),
        abstract="",
        credibility=3,
        meta={"openAccess": r.get("openAccess"), "pii": r.get("pii")},
    )


def search_sciencedirect(query: str, limit: int = 25, year_from: int | None = None) -> list[SearchHit]:
    body: dict[str, Any] = {"qs": query, "show": min(limit, 100), "sortBy": "relevance"}
    if year_from:
        body["date"] = f"{year_from}-2100"
    with httpx.Client(timeout=settings.http_timeout) as c:
        r = c.put(SCIDIR, headers={**_headers(), "Content-Type": "application/json"}, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"ScienceDirect {r.status_code}: {r.text[:200]}")
    data = r.json()
    return [_scidir_hit(x) for x in data.get("results") or []]


def get_abstract(doi: str) -> str:
    try:
        data = get_json(ABSTRACT + normalize_doi(doi), headers=_headers(), retries=0)
    except Exception:  # noqa: BLE001
        return ""
    core = ((data or {}).get("abstracts-retrieval-response") or {}).get("coredata") or {}
    return core.get("dc:description") or ""


def get_fulltext(doi: str) -> str | None:
    """尝试取全文纯文本；无权限返回 None。"""
    try:
        with httpx.Client(timeout=90.0, follow_redirects=True) as c:
            r = c.get(ARTICLE + normalize_doi(doi), headers={**_headers(), "Accept": "text/plain"})
        if r.status_code >= 400:
            return None
        text = r.text.strip()
        # 无权限时 Elsevier 只返回题录/摘要，字数很少
        return text if len(text) > 3000 else None
    except Exception:  # noqa: BLE001
        return None


def ping() -> tuple[bool, str]:
    try:
        hits = search_scopus("metallocene polyethylene blown film", limit=2)
        return True, f"Scopus 可用，测试检索返回 {len(hits)} 条"
    except Exception as e:  # noqa: BLE001
        return False, f"Scopus 不可用：{e}"
