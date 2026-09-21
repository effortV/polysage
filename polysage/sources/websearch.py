"""通用网页搜索：Tavily（有 key 时优先）→ DuckDuckGo（免费）。

用途：价格行情（生意社 / 卓创 / 隆众）、供应商 TDS、行业资讯。所有结果标为“行业网站”可信度 5，
入库后由用户或 LLM 复核后再提级（如供应商官网 TDS → 2）。
"""
from __future__ import annotations

import time

from typing import Any

import httpx

from .. import activity, net
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
    with net.client("https://api.tavily.com", timeout=40) as c:
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


_ENGINE_URL = {"duckduckgo": "https://duckduckgo.com", "brave": "https://search.brave.com", "bing": "https://www.bing.com"}
# 这台网络：Bing 直连最稳（5～8 s），DuckDuckGo / Brave 要走代理且时断时续，放后面兜底
DEFAULT_CHAIN = (("bing", None, 25), ("bing", "wt-wt", 25), ("duckduckgo", "wt-wt", 12), ("brave", "wt-wt", 12))
# 专利等需要 site: 语法的查询：同样 Bing 优先
BING_FIRST_CHAIN = DEFAULT_CHAIN


_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_BING_FRESH = {"d": "ez1", "w": "ez2", "m": "ez3"}


def _bing_html(query: str, limit: int, timelimit: str | None = None) -> list[SearchHit]:
    """直接解析 cn.bing.com 结果页：服务器（无代理）上 ddgs 的 bing 后端常失败，这个最稳；timelimit d/w/m 对应必应的时间筛选。"""
    from urllib.parse import quote

    from bs4 import BeautifulSoup

    url = f"https://cn.bing.com/search?q={quote(query)}&mkt=zh-CN&setlang=zh-hans&count={min(max(limit, 10), 30)}"
    if timelimit in _BING_FRESH:
        url += f"&filters=ex1%3a%22{_BING_FRESH[timelimit]}%22"
    with net.client(url, timeout=25, follow_redirects=True) as c:
        r = c.get(url, headers={"User-Agent": _BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    if r.status_code >= 400:
        raise RuntimeError(f"bing {r.status_code}")
    soup = BeautifulSoup(r.text, "lxml")
    hits: list[SearchHit] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        href = (a.get("href") if a else "") or ""
        if not href.startswith("http"):
            continue
        p = li.select_one(".b_caption p") or li.select_one("p")
        st, cred = _classify(href)
        hits.append(SearchHit(provider="bing", external_id=href, title=a.get_text(" ", strip=True) if a else "", source_type=st, url=href,
                              abstract=p.get_text(" ", strip=True) if p else "", credibility=cred))
        if len(hits) >= limit:
            break
    if not hits:
        raise RuntimeError("bing 无结果")
    return hits


def _ddg(query: str, limit: int, region: str, chain=DEFAULT_CHAIN, timelimit: str | None = None) -> list[SearchHit]:
    from ddgs import DDGS

    # ddgs 9.x 默认 backend="auto" 会轮询 grokipedia 等国内连不上的引擎（每次超时 30 s）。
    # 这边网络到各引擎时好时坏，按顺序试几种“引擎 + 地区”组合，拿到结果就停。
    rows: list[dict] = []
    last: Exception | None = None
    # 先走自带的必应解析（直连、带时间筛选）；失败再走 ddgs 的各引擎
    try:
        return _bing_html(query, limit, timelimit)
    except Exception as e:  # noqa: BLE001
        last = e
    for round_ in range(2):  # 整条链都失败再等 2 s 重来一遍（网络抖动多为瞬时）
        for backend, reg, timeout in chain:
            try:
                with DDGS(timeout=timeout, proxy=net.proxy_for(_ENGINE_URL.get(backend))) as d:
                    rows = d.text(query, max_results=min(limit, 30), region=reg or region, backend=backend, timelimit=timelimit)
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


def search(query: str, limit: int = 10, region: str = "cn-zh", *, bing_first: bool = False, timelimit: str | None = None) -> list[SearchHit]:
    """timelimit: d/w/m/y 只要最近一天/周/月/年的结果（引擎支持时生效）。"""
    with activity.track("search", "web"):
        return _search(query, limit, region, bing_first, timelimit)


def _search(query: str, limit: int, region: str, bing_first: bool = False, timelimit: str | None = None) -> list[SearchHit]:
    if settings.tavily_api_key:
        try:
            return _tavily(query, limit)
        except Exception:  # noqa: BLE001
            pass
    try:
        return _ddg(query, limit, region, BING_FIRST_CHAIN if bing_first else DEFAULT_CHAIN, timelimit)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"网页搜索失败（DuckDuckGo）：{e}")
