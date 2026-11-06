"""OpenAlex 文献检索（https://docs.openalex.org）。

- 可以填多个 api_key（设置页一行一个，存 .env 的 OPENALEX_API_KEYS）：一个失效 / 今天额度用完就自动换下一个，
  全都不能用时再不带 key 试一次；另带 mailto 进礼貌池。
- 摘要以倒排索引形式返回，这里还原为文本。
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .. import activity, net
from ..config import settings
from .base import SearchHit, normalize_doi

API = "https://api.openalex.org/works"
_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}      # key -> {"status": 可用/无效/限流, "until": 冷却到何时, "at": 何时}
BAD_COOLDOWN = 6 * 3600                     # 无效的 key 6 小时内不再用（改了设置会重新检查）
LIMIT_COOLDOWN = 3600                       # 限流 / 额度用完的 key 1 小时后再试


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "已填"


def _mark(key: str, status: str, cooldown: float = 0.0) -> None:
    with _lock:
        _state[key] = {"status": status, "until": time.time() + cooldown, "at": time.time()}


def _usable() -> list[str]:
    now = time.time()
    with _lock:
        return [k for k in settings.openalex_keys if _state.get(k, {}).get("until", 0) <= now]


def _request(url: str, params: dict[str, Any], key: str) -> httpx.Response:
    p = dict(params)
    if settings.openalex_mailto:
        p["mailto"] = settings.openalex_mailto
    if key:
        p["api_key"] = key
    hdrs = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    last: Exception | None = None
    for attempt in range(2):
        try:
            with activity.track("search", "api.openalex.org"):
                with net.client(url, timeout=settings.http_timeout, follow_redirects=True) as c:
                    return c.get(url, params=p, headers=hdrs)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise last or RuntimeError("OpenAlex 请求失败")


def _get(url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """按顺序用可用的 key；401/403 记为无效、429 记为限流，换下一个；都不行最后不带 key 试一次。"""
    last = ""
    for key in _usable() + [""]:
        r = _request(url, params, key)
        if r.status_code == 429:                      # 每秒并发超了也会 429：歇一下同一个 key 再试一次
            time.sleep(1.5)
            r = _request(url, params, key)
        if r.status_code == 200:
            if key:
                _mark(key, "可用")
            return r.json()
        if r.status_code == 404:
            return None
        last = f"HTTP {r.status_code}: {r.text[:200]}"
        if key and r.status_code in (401, 403):
            _mark(key, "无效", BAD_COOLDOWN)
        elif key and r.status_code == 429:
            _mark(key, "限流", LIMIT_COOLDOWN)
        elif r.status_code < 500 and r.status_code not in (401, 403, 429):
            break                                     # 请求本身有问题（400 等），换 key 也没用
    raise RuntimeError(f"OpenAlex {last}")


def check_keys() -> list[tuple[str, str]]:
    """逐个试一次（取 1 条最小结果，几乎不耗额度），记下状态，返回 [(打码的 key, 状态)]。"""
    for key in settings.openalex_keys:
        try:
            r = _request(API, {"per-page": 1, "select": "id"}, key)
            if r.status_code == 429:
                time.sleep(1.5)
                r = _request(API, {"per-page": 1, "select": "id"}, key)
            code = r.status_code
        except Exception:  # noqa: BLE001
            _mark(key, "连不上")
            continue
        if code == 200:
            _mark(key, "可用")
        elif code in (401, 403):
            _mark(key, "无效", BAD_COOLDOWN)
        elif code == 429:
            _mark(key, "限流", LIMIT_COOLDOWN)
        else:
            _mark(key, f"异常 {code}")
    return key_report()


def key_report() -> list[tuple[str, str]]:
    """不发请求：按最近一次使用 / 检查的结果报每个 key 的状态。"""
    with _lock:
        return [(mask(k), _state.get(k, {}).get("status", "未检查")) for k in settings.openalex_keys]



def _abstract(inv: dict[str, list[int]] | None) -> str:
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _hit(w: dict[str, Any]) -> SearchHit:
    authors = ", ".join(
        (a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])[:6]
    )
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    oa = w.get("open_access") or {}
    best_oa = w.get("best_oa_location") or {}
    return SearchHit(
        provider="openalex",
        external_id=(w.get("id") or "").rsplit("/", 1)[-1],
        title=w.get("display_name") or w.get("title") or "",
        source_type="paper",
        authors=authors,
        year=w.get("publication_year"),
        venue=src.get("display_name") or "",
        doi=normalize_doi(w.get("doi") or ""),
        url=(w.get("doi") or loc.get("landing_page_url") or w.get("id") or ""),
        abstract=_abstract(w.get("abstract_inverted_index")),
        oa_pdf_url=best_oa.get("pdf_url") or oa.get("oa_url") or "",
        credibility=3,
        license=(best_oa.get("license") or ""),
        meta={
            "cited_by_count": w.get("cited_by_count"),
            "type": w.get("type"),
            "is_oa": oa.get("is_oa"),
            "concepts": [c.get("display_name") for c in (w.get("concepts") or [])[:6]],
        },
    )


def search(query: str, limit: int = 25, year_from: int | None = None, sort: str = "relevance_score:desc") -> list[SearchHit]:
    params: dict[str, Any] = {
        "search": query,
        "per-page": min(max(limit, 1), 100),
        "sort": sort,
        "select": "id,display_name,title,authorships,publication_year,primary_location,best_oa_location,"
                  "open_access,doi,abstract_inverted_index,cited_by_count,type,concepts",
    }
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if filters:
        params["filter"] = ",".join(filters)
    data = _get(API, params) or {}
    return [_hit(w) for w in data.get("results") or []]


def get_by_doi(doi: str) -> SearchHit | None:
    params: dict[str, Any] = {}
    try:
        data = _get(f"{API}/https://doi.org/{normalize_doi(doi)}", params)
    except Exception:  # noqa: BLE001
        return None
    return _hit(data) if data else None
