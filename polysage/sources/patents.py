"""专利检索：Google Patents（无需密钥）+ 可选 Lens.org（LENS_API_TOKEN）。

Google Patents 的 xhr/query 接口返回 JSON 题录；专利全文页解析摘要 / 权利要求 / 说明书。
国内网络若无法访问 Google，可改用 Lens.org（免费注册令牌）或手动上传专利 PDF。
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from .base import SearchHit, get_json, get_text

GP_QUERY = "https://patents.google.com/xhr/query"
GP_PAGE = "https://patents.google.com/patent/"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def search_google_patents(query: str, limit: int = 20, language: str | None = None) -> list[SearchHit]:
    inner = f"q={quote(query)}&num={min(limit, 100)}"
    if language:
        inner += f"&language={language}"
    data = get_json(GP_QUERY, params={"url": inner, "exp": ""}, headers={"Accept": "application/json"}) or {}
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


def fetch_google_patent(publication_number: str) -> dict[str, Any]:
    """抓取专利页，返回 {title, abstract, claims, description, assignee, url}。"""
    url = GP_PAGE + publication_number.strip() + "/en"
    _, html = get_text(url)
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
    return {
        "title": title,
        "abstract": section("abstract"),
        "claims": section("claims"),
        "description": section("description"),
        "assignee": assignee,
        "url": url,
    }


def search_lens(query: str, limit: int = 20) -> list[SearchHit]:
    """Lens.org 专利检索（需 LENS_API_TOKEN）。"""
    token = os.getenv("LENS_API_TOKEN", "").strip()
    if not token:
        return []
    import httpx

    body = {"query": {"query_string": {"query": query}}, "size": min(limit, 50),
            "include": ["lens_id", "biblio", "abstract", "doc_key"]}
    with httpx.Client(timeout=40) as c:
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
