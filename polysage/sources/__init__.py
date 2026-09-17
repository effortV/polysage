"""统一检索入口：一次查询并行打多个源，去重后返回。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .. import db
from ..config import settings
from .base import SearchHit, dedupe

PROVIDERS: dict[str, str] = {
    "openalex": "OpenAlex（文献）",
    "crossref": "Crossref（文献）",
    "semantic_scholar": "Semantic Scholar（文献）",
    "scopus": "Elsevier Scopus（文献）",
    "sciencedirect": "Elsevier ScienceDirect（文献）",
    "google_patents": "Google Patents（专利）",
    "web": "网页搜索（价格 / TDS / 行业）",
}

DEFAULT_PROVIDERS = ["openalex", "crossref", "semantic_scholar", "scopus", "google_patents"]


def _runner(name: str) -> Callable[[str, int, int | None], list[SearchHit]]:
    if name == "openalex":
        from . import openalex
        return lambda q, n, y: openalex.search(q, n, y)
    if name == "crossref":
        from . import crossref
        return lambda q, n, y: crossref.search(q, n, y)
    if name == "semantic_scholar":
        from . import semantic_scholar
        return lambda q, n, y: semantic_scholar.search(q, n, y)
    if name == "scopus":
        from . import elsevier
        return lambda q, n, y: elsevier.search_scopus(q, n, y)
    if name == "sciencedirect":
        from . import elsevier
        return lambda q, n, y: elsevier.search_sciencedirect(q, n, y)
    if name == "google_patents":
        from . import patents
        return lambda q, n, y: patents.search_google_patents(q, n)
    if name == "web":
        from . import websearch
        return lambda q, n, y: websearch.search(q, n)
    raise KeyError(name)


def search_all(query: str, providers: list[str] | None = None, limit: int = 20,
               year_from: int | None = None) -> tuple[list[SearchHit], dict[str, str]]:
    """返回 (去重后的命中, {provider: 错误信息})。"""
    providers = providers or DEFAULT_PROVIDERS
    if not settings.elsevier_api_key:
        providers = [p for p in providers if p not in ("scopus", "sciencedirect")]
    hits: list[SearchHit] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(providers) or 1)) as ex:
        futs = {ex.submit(_runner(p), query, limit, year_from): p for p in providers}
        for f in as_completed(futs):
            p = futs[f]
            try:
                res = f.result()
                hits.extend(res)
                db.insert("search_log", {"query": query, "provider": p, "n_results": len(res), "created_at": db.now()})
            except Exception as e:  # noqa: BLE001
                errors[p] = str(e)[:300]
                db.insert("search_log", {"query": query, "provider": p, "n_results": 0, "note": errors[p],
                                         "created_at": db.now()})
    merged = dedupe(hits)
    merged.sort(key=lambda h: (h.source_type != "paper", -(h.year or 0)))
    return merged, errors
