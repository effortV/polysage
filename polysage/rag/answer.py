"""带引用的问答：检索 → 组装 [S1]..[Sn] 上下文 → LLM 回答 → 解析引用。"""
from __future__ import annotations

import re
from typing import Any

from .. import llm
from .index import search

CRED_LABEL = {1: "实验数据", 2: "供应商TDS", 3: "期刊文献", 4: "专利", 5: "行业网站", 6: "论坛/公众号"}


def cite_label(src: dict[str, Any]) -> str:
    bits = [src.get("title") or "无题"]
    if src.get("authors"):
        bits.append(src["authors"][:40])
    if src.get("venue"):
        bits.append(src["venue"])
    if src.get("year"):
        bits.append(str(src["year"]))
    bits.append(f"来源:{src.get('provider', '')}")
    bits.append(f"可信度:{CRED_LABEL.get(src.get('credibility', 3), '?')}")
    link = src.get("doi") and f"https://doi.org/{src['doi']}" or src.get("url") or src.get("file_path") or ""
    if link:
        bits.append(link)
    return " | ".join(bits)


def build_context(hits: list[dict[str, Any]], max_chars: int = 9000) -> tuple[str, list[dict[str, Any]]]:
    """把命中块编号为 [S1]...；同一来源多块共用编号。返回 (context_text, citations)。"""
    citations: list[dict[str, Any]] = []
    label_by_source: dict[int, int] = {}
    lines = []
    used = 0
    for h in hits:
        sid = h["source_id"]
        if sid not in label_by_source:
            label_by_source[sid] = len(citations) + 1
            src = h["source"]
            citations.append({"label": f"S{label_by_source[sid]}", "source_id": sid, "title": src.get("title"),
                              "provider": src.get("provider"), "year": src.get("year"), "doi": src.get("doi"),
                              "url": src.get("url"), "file_path": src.get("file_path"),
                              "credibility": src.get("credibility"), "cite": cite_label(src)})
        tag = f"[S{label_by_source[sid]}]"
        piece = f"{tag} {h['text']}"
        if used + len(piece) > max_chars:
            break
        lines.append(piece)
        used += len(piece)
    return "\n\n".join(lines), citations


SYSTEM_QA = (
    "你是包装膜（聚乙烯吹膜）配方研发助手。只依据给出的资料片段回答；每个结论后面用 [S#] 标注出处。"
    "资料不足以回答时明确说“知识库中未找到依据”，不要编造数据或牌号。回答用中文，结构化、简洁。"
)


def answer(question: str, top_k: int = 8, source_types: list[str] | None = None,
           extra_system: str = "") -> dict[str, Any]:
    hits = search(question, top_k=top_k, source_types=source_types)
    context, citations = build_context(hits)
    if not context:
        return {"answer": "知识库中未找到相关资料。请先在“资料检索”页检索并入库，或手动上传文献/TDS。",
                "citations": [], "hits": []}
    messages = [
        {"role": "system", "content": SYSTEM_QA + ("\n" + extra_system if extra_system else "")},
        {"role": "user", "content": f"资料片段：\n{context}\n\n问题：{question}"},
    ]
    res = llm.chat(messages, temperature=0.2, thinking=1024)
    used = set(re.findall(r"\[S(\d+)\]", res.content))
    cits = [c for c in citations if c["label"][1:] in used] or citations
    return {"answer": res.content, "citations": cits, "hits": hits}
