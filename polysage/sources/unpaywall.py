"""Unpaywall：按 DOI 查找合法的开放获取 PDF（免费，需邮箱）。"""
from __future__ import annotations

from ..config import settings
from .base import get_json, normalize_doi

API = "https://api.unpaywall.org/v2/"


def find_oa_pdf(doi: str) -> tuple[str, str]:
    """返回 (pdf_url, license)；找不到返回 ("", "")。"""
    if not settings.unpaywall_email or not doi:
        return "", ""
    try:
        data = get_json(API + normalize_doi(doi), params={"email": settings.unpaywall_email}, retries=0)
    except Exception:  # noqa: BLE001
        return "", ""
    if not data:
        return "", ""
    best = data.get("best_oa_location") or {}
    url = best.get("url_for_pdf") or ""
    if not url:
        for loc in data.get("oa_locations") or []:
            if loc.get("url_for_pdf"):
                url = loc["url_for_pdf"]
                best = loc
                break
    return url, (best.get("license") or "")
