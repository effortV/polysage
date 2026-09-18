"""专利检索：Google Patents（直连）→ 不可达时用搜索引擎的 site:patents.google.com 结果 → 可选 Lens.org（LENS_API_TOKEN）。

国内网络到 Google 常被拦（返回 503 "Sorry"），所以：
- 检索：直连失败一次后 30 分钟内不再试，改用 Bing/DuckDuckGo 检索 patents.google.com 的收录页（拿到公开号、标题、摘要片段）；
- 取文：专利页抓不到时用 PubChem 的专利记录（含英文标题、摘要、申请人、日期；无权利要求与说明书）。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from .. import net
from .base import SearchHit, get_json, get_text

GP_QUERY = "https://patents.google.com/xhr/query"
GP_PAGE = "https://patents.google.com/patent/"
PUBCHEM_VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/patent/{pid}/JSON"
PUBCHEM_PAGE = "https://pubchem.ncbi.nlm.nih.gov/patent/{pid}"
_BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*", "Referer": "https://patents.google.com/",
            "X-Requested-With": "XMLHttpRequest"}
_BLOCK_SECONDS = 1800
_blocked_until = 0.0


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def google_blocked() -> bool:
    return time.time() < _blocked_until


def _mark_blocked() -> None:
    global _blocked_until
    _blocked_until = time.time() + _BLOCK_SECONDS


def _search_google_direct(query: str, limit: int, language: str | None) -> list[SearchHit]:
    inner = f"q={quote(query)}&num={min(limit, 100)}"
    if language:
        inner += f"&language={language}"
    data = get_json(GP_QUERY, params={"url": inner, "exp": ""}, headers=_BROWSER, retries=0) or {}
    hits: list[SearchHit] = []
    for cluster in (data.get("results") or {}).get("cluster") or []:
        for r in cluster.get("result") or []:
            p = r.get("patent") or {}
            pn = p.get("publication_number") or ""
            if not pn:
                continue
            date = p.get("publication_date") or p.get("priority_date") or ""
            hits.append(SearchHit(
                provider="google_patents",
                external_id=pn,
                title=_clean(re.sub(r"<[^>]+>", "", p.get("title") or "")),
                source_type="patent",
                authors=_clean(re.sub(r"<[^>]+>", "", p.get("inventor") or "")),
                year=int(date[:4]) if date[:4].isdigit() else None,
                venue=_clean(re.sub(r"<[^>]+>", "", p.get("assignee") or "")),
                url=GP_PAGE + pn + "/en",
                abstract=_clean(re.sub(r"<[^>]+>", "", p.get("snippet") or "")),
                oa_pdf_url=p.get("pdf") or "",
                credibility=4,
                meta={"priority_date": p.get("priority_date"), "filing_date": p.get("filing_date"),
                      "publication_date": p.get("publication_date")},
            ))
    return hits[:limit]


_PN_IN_URL = re.compile(r"patents\.google\.com/patent/([A-Z]{2}\d{5,}[A-Z]?\d?)")


def _search_via_web(query: str, limit: int) -> list[SearchHit]:
    """用通用搜索引擎检索 patents.google.com 的收录页：URL 里有公开号，标题/摘要片段来自搜索结果。"""
    from . import websearch

    try:
        rows = websearch.search(f"site:patents.google.com {query}", limit=min(max(limit, 10), 30), bing_first=True)
    except Exception:  # noqa: BLE001  搜索引擎也没结果
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for h in rows:
        m = _PN_IN_URL.search(h.url or "")
        if not m:
            continue
        pn = m.group(1)
        key = re.sub(r"[A-Z]\d?$", "", pn)  # 同一专利的 A/B 公开号只留一条
        if key in seen:
            continue
        seen.add(key)
        title = re.sub(r"\s*-\s*Google Patents\s*$", "", h.title or "")
        title = re.sub(r"^\s*" + re.escape(pn) + r"\s*-\s*", "", title)
        hits.append(SearchHit(provider="google_patents", external_id=pn, title=_clean(title), source_type="patent",
                              url=GP_PAGE + pn + "/en", abstract=_clean(h.abstract), credibility=4,
                              meta={"via": "web_search"}))
        if len(hits) >= limit:
            break
    return hits


def search_google_patents(query: str, limit: int = 20, language: str | None = None) -> list[SearchHit]:
    """Google 直连 → 搜索引擎 site: 回退 → Lens（有令牌时）。"""
    hits: list[SearchHit] = []
    if not google_blocked():
        try:
            hits = _search_google_direct(query, limit, language)
        except Exception:  # noqa: BLE001  503 "Sorry" / 超时：30 分钟内不再直连
            _mark_blocked()
    if not hits:
        hits = _search_via_web(query, limit)
    if not hits and os.getenv("LENS_API_TOKEN", "").strip():
        hits = search_lens(query, limit)
    return hits


def _fetch_google_page(publication_number: str) -> dict[str, Any]:
    url = GP_PAGE + publication_number.strip() + "/en"
    _, html = get_text(url, headers={"User-Agent": _BROWSER["User-Agent"]})
    soup = BeautifulSoup(html, "lxml")
    title = _clean((soup.find("meta", attrs={"name": "DC.title"}) or {}).get("content", "") or
                   (soup.title.string if soup.title else ""))

    def section(name: str) -> str:
        sec = soup.find("section", attrs={"itemprop": name})
        if not sec:
            return ""
        return _clean(sec.get_text(" "))

    assignee = ""
    a = soup.find("dd", attrs={"itemprop": "assigneeOriginal"}) or soup.find("dd", attrs={"itemprop": "assigneeCurrent"})
    if a:
        assignee = _clean(a.get_text(" "))
    if not (section("abstract") or section("claims")):
        raise RuntimeError("专利页没有解析到内容（可能被拦截）")
    return {"title": title, "abstract": section("abstract"), "claims": section("claims"),
            "description": section("description"), "assignee": assignee, "url": url, "source": "google",
            "note": "已抓取专利全文"}


def pubchem_patent_id(publication_number: str) -> str:
    """CN102167855A → CN-102167855-A（PubChem 的专利 ID 写法）。"""
    pn = publication_number.strip().replace("-", "").upper()
    m = re.match(r"^([A-Z]{2})(\d+)([A-Z]\d?)?$", pn)
    if not m:
        return pn
    cc, num, kind = m.groups()
    return f"{cc}-{num}-{kind}" if kind else f"{cc}-{num}"


def _pc_text(section: dict[str, Any]) -> str:
    out: list[str] = []
    for info in section.get("Information") or []:
        v = info.get("Value") or {}
        for sw in v.get("StringWithMarkup") or []:
            if sw.get("String"):
                out.append(sw["String"])
        if v.get("DateISO8601"):
            out.extend(str(x) for x in v["DateISO8601"])
    return _clean(" ".join(out))


def fetch_pubchem_patent(publication_number: str) -> dict[str, Any]:
    """PubChem 专利记录：英文（机器翻译）标题与摘要、申请人、发明人、日期；没有权利要求和说明书。"""
    pid = pubchem_patent_id(publication_number)
    data = get_json(PUBCHEM_VIEW.format(pid=pid), retries=1) or {}
    rec = data.get("Record") or {}
    fields: dict[str, str] = {}

    def walk(sections: list[dict[str, Any]]) -> None:
        for s in sections:
            head = s.get("TOCHeading") or ""
            if head in ("Abstract", "Inventor", "Assignee", "Publication Date", "Priority Date", "Filing Date"):
                fields[head] = _pc_text(s)
            walk(s.get("Section") or [])

    walk(rec.get("Section") or [])
    title = re.sub(r"^\[Translated\]\s*", "", rec.get("RecordTitle") or "")
    abstract = re.sub(r"^\[Translated\]\s*", "", fields.get("Abstract", ""))
    if not (title or abstract):
        raise RuntimeError(f"PubChem 无此专利记录：{pid}")
    return {"title": _clean(title), "abstract": abstract, "claims": "", "description": "",
            "assignee": fields.get("Assignee", ""), "inventor": fields.get("Inventor", ""),
            "publication_date": fields.get("Publication Date", ""), "priority_date": fields.get("Priority Date", ""),
            "url": PUBCHEM_PAGE.format(pid=pid), "source": "pubchem",
            "note": "Google 专利页不可达，已用 PubChem 取到标题与摘要（无权利要求/说明书）"}


def fetch_google_patent(publication_number: str) -> dict[str, Any]:
    """抓取专利内容：Google 专利页（全文）→ 不可达时 PubChem（摘要）。返回 {title, abstract, claims, description, assignee, url, note}。"""
    if not google_blocked():
        try:
            return _fetch_google_page(publication_number)
        except Exception:  # noqa: BLE001
            _mark_blocked()
    return fetch_pubchem_patent(publication_number)


def search_lens(query: str, limit: int = 20) -> list[SearchHit]:
    """Lens.org 专利检索（需 LENS_API_TOKEN）。"""
    token = os.getenv("LENS_API_TOKEN", "").strip()
    if not token:
        return []
    import httpx

    body = {"query": {"query_string": {"query": query}}, "size": min(limit, 50),
            "include": ["lens_id", "biblio", "abstract", "doc_key"]}
    with net.client("https://api.lens.org", timeout=40) as c:
        r = c.post("https://api.lens.org/patent/search", headers={"Authorization": f"Bearer {token}"}, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Lens {r.status_code}: {r.text[:200]}")
    hits = []
    for d in r.json().get("data") or []:
        bib = d.get("biblio") or {}
        titles = ((bib.get("invention_title") or [{}])[0]).get("text", "")
        parties = bib.get("parties") or {}
        applicants = ", ".join(x.get("extracted_name", {}).get("value", "") for x in (parties.get("applicants") or [])[:3])
        abst = ((d.get("abstract") or [{}])[0]).get("text", "")
        pubs = (bib.get("publication_reference") or {})
        date = pubs.get("date") or ""
        hits.append(SearchHit(provider="lens", external_id=d.get("lens_id", ""), title=titles, source_type="patent",
                              venue=applicants, year=int(date[:4]) if date[:4].isdigit() else None,
                              url=f"https://www.lens.org/lens/patent/{d.get('lens_id', '')}", abstract=abst,
                              credibility=4, meta={"doc_key": d.get("doc_key")}))
    return hits
