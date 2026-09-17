"""入库流水线：检索命中 / 本地文件 / 网页 → sources + chunks（+ 向量）。"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .. import db, llm
from ..config import UPLOAD_DIR
from ..sources.base import SearchHit, normalize_doi
from .chunker import chunk_text
from .parsers import is_cnki_export, parse_cnki_export, parse_file


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def _title_key(title: str) -> str:
    return re.sub(r"\W+", "", (title or "").lower())[:120]


def _store_chunks(source_id: int, text: str, do_embed: bool = True) -> int:
    chunks = chunk_text(text) if text else []
    vecs = llm.embed(chunks) if (do_embed and chunks) else None
    with db.tx() as conn:
        conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        for i, c in enumerate(chunks):
            emb = vecs[i].astype(np.float32).tobytes() if vecs is not None else None
            conn.execute("INSERT INTO chunks (source_id, ord, text, embedding, n_chars) VALUES (?,?,?,?,?)",
                         (source_id, i, c, emb, len(c)))
        conn.execute("UPDATE sources SET n_chunks=? WHERE id=?", (len(chunks), source_id))
    return len(chunks)


def _source_text(title: str, abstract: str, body: str) -> str:
    parts = [f"# {title}"] if title else []
    if abstract:
        parts.append("摘要：" + abstract)
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def ingest_hit(hit: SearchHit, fetch_fulltext: bool = True) -> tuple[int, str]:
    """把检索命中入库。返回 (source_id, 说明)。已存在则补充全文。"""
    content_hash = _hash(hit.provider if not hit.doi else "doi", hit.doi or hit.external_id or _title_key(hit.title))
    existing = db.source_by_hash(content_hash) or (db.source_by_doi(hit.doi) if hit.doi else None)
    body, file_path, note = "", "", ""
    if fetch_fulltext:
        body, file_path, note = _try_fulltext(hit)
    text = _source_text(hit.title, hit.abstract, body)
    if existing:
        if body and (existing.get("n_chunks") or 0) <= 2:
            db.update("sources", existing["id"], {"file_path": file_path or existing.get("file_path"),
                                                  "meta_json": db.dumps({**db.loads(existing.get("meta_json"), {}),
                                                                         "fulltext": True})})
            n = _store_chunks(existing["id"], text)
            return existing["id"], f"已存在，补充全文（{n} 块）"
        return existing["id"], "已存在"
    sid = db.insert("sources", {
        "external_id": hit.external_id, "provider": hit.provider, "source_type": hit.source_type,
        "title": hit.title, "authors": hit.authors, "year": hit.year, "venue": hit.venue,
        "doi": normalize_doi(hit.doi), "url": hit.url, "file_path": file_path, "abstract": hit.abstract,
        "credibility": hit.credibility, "license": hit.license, "content_hash": content_hash,
        "meta_json": db.dumps({**hit.meta, "fulltext": bool(body), "oa_pdf_url": hit.oa_pdf_url}),
        "retrieved_at": db.now(),
    })
    n = _store_chunks(sid, text)
    return sid, f"入库 {n} 块" + (f"；{note}" if note else "")


def _try_fulltext(hit: SearchHit) -> tuple[str, str, str]:
    """尽力获取全文：专利页 → OA PDF（自带 / Unpaywall）→ Elsevier 全文。返回 (text, file_path, note)。"""
    from ..sources import fetch as fetcher

    if hit.source_type == "patent" and hit.provider == "google_patents":
        try:
            from ..sources.patents import fetch_google_patent

            p = fetch_google_patent(hit.external_id)
            body = "\n\n".join(x for x in [
                ("摘要：" + p["abstract"]) if p["abstract"] else "",
                ("权利要求：" + p["claims"]) if p["claims"] else "",
                ("说明书：" + p["description"][:60000]) if p["description"] else "",
            ] if x)
            if p.get("assignee") and not hit.venue:
                hit.venue = p["assignee"]
            return body, "", "已抓取专利全文"
        except Exception as e:  # noqa: BLE001
            return "", "", f"专利页抓取失败：{str(e)[:80]}"
    if hit.source_type in ("web", "tds", "price"):
        try:
            r = fetcher.fetch_url(hit.url)
            return r["text"][:120000], r["file_path"], "已抓取网页正文"
        except Exception as e:  # noqa: BLE001
            return "", "", f"网页抓取失败：{str(e)[:80]}"
    # 文献：OA PDF
    pdf_url = hit.oa_pdf_url
    if not pdf_url and hit.doi:
        from ..sources.unpaywall import find_oa_pdf

        pdf_url, lic = find_oa_pdf(hit.doi)
        if lic and not hit.license:
            hit.license = lic
    if pdf_url:
        try:
            r = fetcher.fetch_url(pdf_url)
            if r["text"] and len(r["text"]) > 1500:
                return r["text"], r["file_path"], "已下载 OA 全文"
        except Exception as e:  # noqa: BLE001
            note = f"OA 下载失败：{str(e)[:60]}"
        else:
            note = "OA 链接无正文"
    else:
        note = "无 OA 全文（可手动上传 PDF）"
    if hit.doi:
        from ..config import settings

        if settings.elsevier_api_key:
            from ..sources.elsevier import get_fulltext

            ft = get_fulltext(hit.doi)
            if ft:
                return ft, "", "Elsevier 全文"
    return "", "", note


def ingest_file(path: Path, *, title: str = "", provider: str = "manual", source_type: str = "paper",
                credibility: int = 3, doi: str = "", url: str = "", authors: str = "", year: int | None = None,
                venue: str = "", notes: str = "", copy_to_uploads: bool = True) -> list[tuple[int, str]]:
    """本地文件入库；CNKI/万方导出文本会拆成多条题录。返回 [(source_id, 说明)]。"""
    path = Path(path)
    if copy_to_uploads and UPLOAD_DIR not in path.parents:
        dest = UPLOAD_DIR / path.name
        if dest.exists() and dest.stat().st_size != path.stat().st_size:
            dest = UPLOAD_DIR / f"{path.stem}_{hashlib.md5(str(path).encode()).hexdigest()[:6]}{path.suffix}"
        shutil.copy2(path, dest)
        path = dest
    text, meta = parse_file(path)
    if path.suffix.lower() in (".txt", ".net", ".ris") and is_cnki_export(text):
        return ingest_cnki_records(parse_cnki_export(text), origin=path.name)
    title = title or meta.get("title") or path.stem
    content_hash = _hash("file", hashlib.sha256(path.read_bytes()).hexdigest())
    existing = db.source_by_hash(content_hash)
    if existing:
        return [(existing["id"], "文件已存在")]
    sid = db.insert("sources", {
        "external_id": path.name, "provider": provider, "source_type": source_type, "title": title,
        "authors": authors or meta.get("authors", ""), "year": year, "venue": venue, "doi": normalize_doi(doi),
        "url": url, "file_path": str(path), "abstract": "", "credibility": credibility,
        "content_hash": content_hash, "meta_json": db.dumps({**meta, "notes": notes, "fulltext": True}),
        "retrieved_at": db.now(),
    })
    n = _store_chunks(sid, _source_text(title, "", text))
    return [(sid, f"入库 {n} 块")]


def ingest_cnki_records(records: list[dict[str, str]], origin: str = "") -> list[tuple[int, str]]:
    out = []
    for r in records:
        title = r.get("title", "")
        year = r.get("year") or (r.get("pubtime") or "")[:4]
        hit = SearchHit(provider="cnki_export", external_id=r.get("doi") or title, title=title, source_type="paper",
                        authors=r.get("author", ""), year=int(year) if str(year).isdigit() else None,
                        venue=r.get("source", ""), doi=r.get("doi", ""), url=r.get("url", ""),
                        abstract=r.get("summary", ""), credibility=3,
                        meta={"keywords": r.get("keyword", ""), "origin_file": origin, "db": r.get("srcdatabase", "")})
        out.append(ingest_hit(hit, fetch_fulltext=False))
    return out


def ingest_text(text: str, *, title: str, provider: str = "manual", source_type: str = "web", url: str = "",
                credibility: int = 5, meta: dict[str, Any] | None = None) -> tuple[int, str]:
    content_hash = _hash("text", url or title, text[:2000])
    existing = db.source_by_hash(content_hash)
    if existing:
        return existing["id"], "已存在"
    sid = db.insert("sources", {
        "external_id": url or title, "provider": provider, "source_type": source_type, "title": title,
        "url": url, "credibility": credibility, "content_hash": content_hash,
        "meta_json": db.dumps({**(meta or {}), "fulltext": True}), "retrieved_at": db.now(),
    })
    n = _store_chunks(sid, _source_text(title, "", text))
    return sid, f"入库 {n} 块"


def ingest_url(url: str, *, source_type: str = "web", credibility: int = 5, title: str = "") -> tuple[int, str]:
    from ..sources import fetch as fetcher

    r = fetcher.fetch_url(url)
    if not r["text"]:
        raise RuntimeError("未能提取到正文")
    return ingest_text(r["text"], title=title or r["title"] or url, provider="web_fetch", source_type=source_type,
                       url=url, credibility=credibility, meta={"file_path": r["file_path"]})


def embed_missing(batch: int = 64) -> int:
    """为尚无向量的切片补建向量（配置密钥后调用）。返回处理数量。"""
    rows = db.q("SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id")
    if not rows:
        return 0
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = llm.embed([r["text"] for r in part])
        if vecs is None:
            break
        with db.tx() as conn:
            for r, v in zip(part, vecs):
                conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (v.astype(np.float32).tobytes(), r["id"]))
        done += len(part)
    return done


def delete_source(source_id: int) -> None:
    db.delete("sources", source_id)
