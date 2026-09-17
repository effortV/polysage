"""Crossref 文献元数据检索（免费，https://api.crossref.org）。"""
from __future__ import annotations

import re
from typing import Any

from ..config import settings
from .base import SearchHit, get_json, normalize_doi

API = "https://api.crossref.org/works"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def _hit(it: dict[str, Any]) -> SearchHit:
    title = " ".join(it.get("title") or [])
    authors = ", ".join(
        f"{a.get('given', '')} {a.get('family', '')}".strip() for a in (it.get("author") or [])[:6]
    )
    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        parts = (it.get(k) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
            break
    pdf = ""
    for ln in it.get("link") or []:
        if "pdf" in (ln.get("content-type") or "") and ln.get("URL"):
            pdf = ln["URL"]
            break
    lic = ""
    if it.get("license"):
        lic = (it["license"][0] or {}).get("URL", "")
    return SearchHit(
        provider="crossref",
        external_id=normalize_doi(it.get("DOI") or ""),
        title=_strip_tags(title),
        source_type="paper",
        authors=authors,
        year=year,
        venue=" ".join(it.get("container-title") or []),
        doi=normalize_doi(it.get("DOI") or ""),
        url=it.get("URL") or "",
        abstract=_strip_tags(it.get("abstract") or ""),
        oa_pdf_url=pdf,
        credibility=3,
        license=lic,
        meta={"type": it.get("type"), "publisher": it.get("publisher"), "cited": it.get("is-referenced-by-count")},
    )


def search(query: str, limit: int = 20, year_from: int | None = None) -> list[SearchHit]:
    params: dict[str, Any] = {"query": query, "rows": min(limit, 50), "select": "DOI,title,author,issued,published-print,"
                              "published-online,created,container-title,URL,abstract,link,license,type,publisher,"
                              "is-referenced-by-count"}
    if year_from:
        params["filter"] = f"from-pub-date:{year_from}"
    if settings.openalex_mailto:
        params["mailto"] = settings.openalex_mailto
    data = get_json(API, params=params) or {}
    return [_hit(it) for it in ((data.get("message") or {}).get("items") or [])]
