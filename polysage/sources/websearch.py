"""通用网页搜索：Tavily（有 key 时优先）→ DuckDuckGo（免费）。

用途：价格行情（生意社 / 卓创 / 隆众）、供应商 TDS、行业资讯。所有结果标为“行业网站”可信度 5，
入库后由用户或 LLM 复核后再提级（如供应商官网 TDS → 2）。
"""
from __future__ import annotations

import time

from typing import Any

import httpx

from .. import activity
from ..config import settings
from .base import SearchHit

TDS_DOMAINS = ("dow.com", "exxonmobilchemical.com", "borealisgroup.com", "lyondellbasell.com", "sabic.com",
               "mitsuichemicals.com", "sinopec.com", "petrochina.com", "lg.com", "hanwha", "sk.com",
               "chevronphillips", "basell", "ineos.com", "nova", "formosa")
PRICE_DOMAINS = ("100ppi.com", "sci99.com", "oilchem.net", "315i.com", "baiinfo.com", "jlc.com", "chem366", "pp.cn")


def _classify(url: str) -> tuple[str, int]:
    u = (url or "").lower()
    if any(d in u for d in TDS_DOMAINS):
        return "tds", 2
    if any(d in u for d in PRICE_DOMAINS):
        return "price", 5
    return "web", 5


def _tavily(query: str, limit: int) -> list[SearchHit]:
    with httpx.Client(timeout=40) as c:
        r = c.post("https://api.tavily.com/search", json={
            "api_key": settings.tavily_api_key, "query": query, "max_results": min(limit, 20),
            "search_depth": "basic", "include_answer": False,
        })
    if r.status_code >= 400:
        raise RuntimeError(f"Tavily {r.status_code}: {r.text[:200]}")
    hits = []
    for it in r.json().get("results") or []:
        st, cred = _classify(it.get("url", ""))
        hits.append(SearchHit(provider="tavily", external_id=it.get("url", ""), title=it.get("title", ""),
                              source_type=st, url=it.get("url", ""), abstract=it.get("content", ""),
                              credibility=cred, meta={"score": it.get("score")}))
    return hits


DEFAULT_CHAIN = (("duckduckgo", None, 12), ("duckduckgo", "wt-wt", 12), ("bing", "wt-wt", 25),
                 ("brave", "wt-wt", 12), ("bing", None, 25))
# 专利等需要 site: 语法的查询：Bing 对 site: 和中文最稳，放前面
BING_FIRST_CHAIN = (("bing", "wt-wt", 25), ("bing", None, 25), ("duckduckgo", "wt-wt", 12), ("brave", "wt-wt", 12))


def _ddg(query: str, limit: int, region: str, chain=DEFAULT_CHAIN) -> list[SearchHit]:
    from ddgs import DDGS

    # ddgs 9.x 默认 backend="auto" 会轮询 grokipedia 等国内连不上的引擎（每次超时 30 s）。
    # 这边网络到各引擎时好时坏，按顺序试几种“引擎 + 地区”组合，拿到结果就停。
    rows: list[dict] = []
    last: Exception | None = None
    for round_ in range(2):  # 整条链都失败再等 2 s 重来一遍（网络抖动多为瞬时）
        for backend, reg, timeout in chain:
            try:
                with DDGS(timeout=timeout) as d:
                    rows = d.text(query, max_results=min(limit, 30), region=reg or region, backend=backend)
            except Exception as e:  # noqa: BLE001
                last = e
                continue
            if rows:
                break
        if rows:
            break
        time.sleep(2)
    if not rows:
        raise RuntimeError(f"所有搜索引擎都没返回结果：{last}")
    hits = []
    for it in rows:
        url = it.get("href") or it.get("url") or ""
        st, cred = _classify(url)
        hits.append(SearchHit(provider="duckduckgo", external_id=url, title=it.get("title", ""),
                              source_type=st, url=url, abstract=it.get("body", ""), credibility=cred))
    return hits


def search(query: str, limit: int = 10, region: str = "cn-zh", *, bing_first: bool = False) -> list[SearchHit]:
    with activity.track("search", "web"):
        return _search(query, limit, region, bing_first)


def _search(query: str, limit: int, region: str, bing_first: bool = False) -> list[SearchHit]:
    if settings.tavily_api_key:
        try:
            return _tavily(query, limit)
        except Exception:  # noqa: BLE001
            pass
    try:
        return _ddg(query, limit, region, BING_FIRST_CHAIN if bing_first else DEFAULT_CHAIN)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"网页搜索失败（DuckDuckGo）：{e}")
