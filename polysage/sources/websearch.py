"""通用网页搜索：Tavily（有 key 时优先）→ DuckDuckGo（免费）。

用途：价格行情（生意社 / 卓创 / 隆众）、供应商 TDS、行业资讯。所有结果标为“行业网站”可信度 5，
入库后由用户或 LLM 复核后再提级（如供应商官网 TDS → 2）。
"""
from __future__ import annotations

import re
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


_FRESH_BOCHA = {"d": "oneDay", "w": "oneWeek", "m": "oneMonth", "y": "oneYear"}


def _bocha(query: str, limit: int, timelimit: str | None = None, timeout: float = 20.0) -> list[SearchHit]:
    """博查搜索 API（国内，按次计费）：返回中文站点的标题 / 网址 / 摘要 / 发布时间。"""
    payload: dict[str, Any] = {"query": query, "count": min(max(limit, 5), 20), "summary": True}
    if timelimit in _FRESH_BOCHA:
        payload["freshness"] = _FRESH_BOCHA[timelimit]
    with net.client("https://api.bochaai.com", timeout=timeout) as c:
        r = c.post("https://api.bochaai.com/v1/web-search", json=payload,
                   headers={"Authorization": f"Bearer {settings.bocha_api_key}", "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"bocha {r.status_code}：{r.text[:80]}")
    pages = (((r.json() or {}).get("data") or {}).get("webPages") or {}).get("value") or []
    hits = []
    for it in pages[:limit]:
        url = it.get("url") or ""
        if not url.startswith("http"):
            continue
        st, cred = _classify(url)
        hits.append(SearchHit(provider="bocha", external_id=url, title=(it.get("name") or "")[:120], source_type=st, url=url,
                              abstract=(it.get("summary") or it.get("snippet") or "")[:500], credibility=cred,
                              year=_year_of(it.get("datePublished") or it.get("dateLastCrawled") or "")))
    if not hits:
        raise RuntimeError("bocha 无结果")
    return hits


def _zhipu(query: str, limit: int, timelimit: str | None = None, timeout: float = 20.0) -> list[SearchHit]:
    """智谱开放平台的 Web Search API（国内）。"""
    payload = {"search_engine": "search_std", "search_query": query, "count": min(max(limit, 5), 20)}
    if timelimit in ("d", "w", "m", "y"):
        payload["search_recency_filter"] = {"d": "oneDay", "w": "oneWeek", "m": "oneMonth", "y": "oneYear"}[timelimit]
    with net.client("https://open.bigmodel.cn", timeout=timeout) as c:
        r = c.post("https://open.bigmodel.cn/api/paas/v4/web_search", json=payload,
                   headers={"Authorization": f"Bearer {settings.zhipu_api_key}", "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"zhipu {r.status_code}：{r.text[:80]}")
    data = r.json() or {}
    rows = data.get("search_result") or data.get("data") or []
    hits = []
    for it in rows[:limit]:
        url = it.get("link") or it.get("url") or ""
        if not url.startswith("http"):
            continue
        st, cred = _classify(url)
        hits.append(SearchHit(provider="zhipu", external_id=url, title=(it.get("title") or "")[:120], source_type=st, url=url,
                              abstract=(it.get("content") or it.get("snippet") or "")[:500], credibility=cred,
                              year=_year_of(it.get("publish_date") or "")))
    if not hits:
        raise RuntimeError("zhipu 无结果")
    return hits


def _year_of(text: str) -> int | None:
    m = re.search(r"(20\d{2})", str(text or ""))
    return int(m.group(1)) if m else None


def _bing_html(query: str, limit: int, timelimit: str | None = None, timeout: float = 12.0) -> list[SearchHit]:
    """直接解析 cn.bing.com 结果页：服务器（无代理）上 ddgs 的 bing 后端常失败，这个最稳；timelimit d/w/m 对应必应的时间筛选。"""
    from urllib.parse import quote

    from bs4 import BeautifulSoup

    url = f"https://cn.bing.com/search?q={quote(query)}&mkt=zh-CN&setlang=zh-hans&count={min(max(limit, 10), 30)}"
    if timelimit in _BING_FRESH:
        url += f"&filters=ex1%3a%22{_BING_FRESH[timelimit]}%22"
    with net.client(url, timeout=timeout, follow_redirects=True) as c:
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
    if not _relevant(query, hits):
        raise RuntimeError("bing 疑似降级结果（对爬虫返回无关页面）")
    return hits


def _relevant(query: str, hits: list[SearchHit], min_ratio: float = 0.3) -> bool:
    """必应/百度对可疑来源会返回“首字百科”之类的无关结果：要求至少三成结果的标题或摘要含查询里的某个词。"""
    toks = [t for t in re.split(r"\s+", query.replace("site:", " ")) if len(t) >= 2 and not t.startswith("site:")]
    toks = [t for t in toks if "." not in t]  # 去掉域名
    if not toks:
        return True
    ok = sum(1 for h in hits if any(t in (h.title + " " + h.abstract) for t in toks))
    return ok >= max(1, int(len(hits) * min_ratio))


_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.0 Mobile/15E148 Safari/604.1")
_BAIDU_MU = re.compile(r'"mu"\s*:\s*"([^"]+)"')
_BAIDU_SKIP = ("recommend_list.baidu.com", "m.baidu.com/from=", "baidu.com/search", "/sf?")


def _baidu_html(query: str, limit: int, timelimit: str | None = None, timeout: float = 12.0) -> list[SearchHit]:
    """百度移动版：结果项的真实网址在 data-log 的 mu 字段里。

    服务器这个 IP 上必应会降级（只按第一个词返回百科）、搜狗 403、360 要验证码，百度移动版还能出正常结果，
    而且出来的多是 1688 / 百度爱采购这类带报价和厂家的页面，正合用。
    """
    import json
    from urllib.parse import quote

    from bs4 import BeautifulSoup

    url = f"https://m.baidu.com/s?word={quote(query)}"
    if timelimit in ("d", "w", "m", "y"):
        days = {"d": 1, "w": 7, "m": 31, "y": 365}[timelimit]
        end = int(time.time())
        url += f"&gpc=stf%3D{end - days * 86400}%2C{end}%7Cstftype%3D1"
    # 头要像真手机浏览器：httpx 默认的 accept/user-agent 会被百度判成机器人（返回“百度安全验证”）
    headers = {"User-Agent": _UA_MOBILE, "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
               "Accept-Encoding": "gzip, deflate, br"}
    with net.client(url, timeout=timeout, follow_redirects=True, headers=headers) as c:
        c.get("https://m.baidu.com/")                           # 先拿一次 cookie，直接搜会被挡
        r = c.get(url, headers={"Referer": "https://m.baidu.com/"})
    if r.status_code >= 400:
        raise RuntimeError(f"baidu {r.status_code}")
    soup = BeautifulSoup(r.text, "lxml")
    hits: list[SearchHit] = []
    for it in soup.select("div.c-result"):
        log = it.get("data-log") or ""
        href = ""
        if log:
            try:
                href = (json.loads(log) or {}).get("mu") or ""
            except (ValueError, TypeError):
                m = _BAIDU_MU.search(log)
                href = m.group(1) if m else ""
        if not href.startswith("http") or any(k in href for k in _BAIDU_SKIP):
            continue
        t = it.select_one("[class*=title], h3, .c-title")
        title = t.get_text(" ", strip=True) if t else ""
        body = it.get_text(" ", strip=True)
        if title and body.startswith(title):
            body = body[len(title):]
        st, cred = _classify(href)
        hits.append(SearchHit(provider="baidu", external_id=href, title=title[:120], source_type=st, url=href,
                              abstract=body[:300], credibility=cred))
        if len(hits) >= limit:
            break
    if not hits:
        raise RuntimeError("baidu 无结果")
    if not _relevant(query, hits):
        raise RuntimeError("baidu 疑似降级结果")
    return hits


_SOGOU_FRESH = {"d": "1", "w": "2", "m": "3"}
_JS_REDIRECT = re.compile(r"""(?:location\.replace|window\.location(?:\.href)?\s*=|URL=)\s*\(?["']?(https?://[^"'\s)]+)""")


def _sogou_html(query: str, limit: int, timelimit: str | None = None, timeout: float = 12.0) -> list[SearchHit]:
    """搜狗网页搜索：服务器（国内 IP、无代理）上最稳的一家，支持一天/一周/一月内筛选；结果是跳转链接，逐个解析成真实网址。"""
    from urllib.parse import quote

    from bs4 import BeautifulSoup

    url = f"https://www.sogou.com/web?query={quote(query)}"
    if timelimit in _SOGOU_FRESH:
        url += f"&tsn={_SOGOU_FRESH[timelimit]}"
    hits: list[SearchHit] = []
    with net.client(url, timeout=timeout, follow_redirects=True) as c:
        r = c.get(url, headers={"User-Agent": _BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        if r.status_code >= 400:
            raise RuntimeError(f"sogou {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        for it in soup.select("div.vrwrap, div.rb"):
            a = it.select_one("h3 a")
            if not a:
                continue
            href = a.get("href") or ""
            if href.startswith("/link?"):
                # 搜狗跳转页是 JS 跳转：<script>window.location.replace("真实网址")</script>
                try:
                    rr = c.get("https://www.sogou.com" + href, headers={"User-Agent": _BING_UA})
                    m = _JS_REDIRECT.search(rr.text)
                    href = m.group(1) if m else str(rr.url)
                except Exception:  # noqa: BLE001
                    continue
            if not href.startswith("http") or "sogou.com" in href:
                continue
            snippet = it.select_one("p.str-text, p.str_info, div.ft, p")
            st, cred = _classify(href)
            hits.append(SearchHit(provider="sogou", external_id=href, title=a.get_text(" ", strip=True), source_type=st, url=href,
                                  abstract=snippet.get_text(" ", strip=True) if snippet else "", credibility=cred))
            if len(hits) >= limit:
                break
    if not hits:
        raise RuntimeError("sogou 无结果")
    return hits


def _so360_html(query: str, limit: int, timeout: float = 12.0) -> list[SearchHit]:
    """360 搜索兜底（没有可靠的时间筛选，靠页面日期过滤）。"""
    from urllib.parse import quote

    from bs4 import BeautifulSoup

    url = f"https://www.so.com/s?q={quote(query)}"
    with net.client(url, timeout=timeout, follow_redirects=True) as c:
        r = c.get(url, headers={"User-Agent": _BING_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    if r.status_code >= 400:
        raise RuntimeError(f"360 {r.status_code}")
    soup = BeautifulSoup(r.text, "lxml")
    hits: list[SearchHit] = []
    for it in soup.select("li.res-list"):
        a = it.select_one("h3 a")
        if not a:
            continue
        href = a.get("data-mdurl") or a.get("data-url") or a.get("href") or ""
        if not href.startswith("http") or "so.com" in href:
            continue
        snippet = it.select_one("p.res-desc, .res-rich, p")
        st, cred = _classify(href)
        hits.append(SearchHit(provider="360", external_id=href, title=a.get_text(" ", strip=True), source_type=st, url=href,
                              abstract=snippet.get_text(" ", strip=True) if snippet else "", credibility=cred))
        if len(hits) >= limit:
            break
    if not hits:
        raise RuntimeError("360 无结果")
    return hits


def _ddg(query: str, limit: int, region: str, chain=DEFAULT_CHAIN, timelimit: str | None = None) -> list[SearchHit]:
    from ddgs import DDGS

    # ddgs 9.x 默认 backend="auto" 会轮询 grokipedia 等国内连不上的引擎（每次超时 30 s）。
    # 这边网络到各引擎时好时坏，按顺序试几种“引擎 + 地区”组合，拿到结果就停。
    rows: list[dict] = []
    last: Exception | None = None
    # 先走自带的解析后端：必应（住宅网络好用）→ 搜狗（服务器上最稳，带一周内筛选）→ 360；都不行再走 ddgs 的各引擎
    for fn in (lambda: _bing_html(query, limit, timelimit), lambda: _sogou_html(query, limit, timelimit), lambda: _so360_html(query, limit)):
        try:
            return fn()
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
    """必应 → 百度移动 → 搜狗 → 360（都是自带解析，直连、各 ~10 s）；ddgs 那套在国内服务器上基本不通，
    只有环境变量 WEBSEARCH_USE_DDGS=1 时才兜底，免得一条查询卡好几分钟。"""
    import os

    errors: list[str] = []
    # 有搜索 API 的 key 就先走 API：服务器 IP 上免费引擎被降级/封禁时，这是唯一稳的路
    for name, key, fn in (("tavily", settings.tavily_api_key, lambda: _tavily(query, limit)),
                          ("bocha", settings.bocha_api_key, lambda: _bocha(query, limit, timelimit)),
                          ("zhipu", settings.zhipu_api_key, lambda: _zhipu(query, limit, timelimit))):
        if not key:
            continue
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}:{str(e)[:40]}")
    for name, fn in (("bing", lambda: _bing_html(query, limit, timelimit)),
                     ("baidu", lambda: _baidu_html(query, limit, timelimit)),
                     ("sogou", lambda: _sogou_html(query, limit, timelimit)),
                     ("360", lambda: _so360_html(query, limit))):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}:{str(e)[:40]}")
    if os.getenv("WEBSEARCH_USE_DDGS") == "1":
        try:
            return _ddg(query, limit, region, BING_FIRST_CHAIN if bing_first else DEFAULT_CHAIN, timelimit)
        except Exception as e:  # noqa: BLE001
            errors.append(f"ddgs:{str(e)[:40]}")
    hint = ""
    if not (settings.bocha_api_key or settings.zhipu_api_key or settings.tavily_api_key):
        hint = "。免费引擎对这台服务器的 IP 要么降级要么要验证码，想稳定检索就在 .env 里配一个搜索 API 的 key（BOCHA_API_KEY 或 ZHIPU_API_KEY）后重启"
    raise RuntimeError("网页搜索无结果（" + "；".join(errors) + "）" + hint)
