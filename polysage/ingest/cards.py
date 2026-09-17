"""用 LLM 把资料整理成卡片（文献卡 / 专利卡 / 材料卡），字段按内部技术路线 3.4 模板。"""
from __future__ import annotations

from typing import Any

from .. import db, kb, llm
from ..rag.answer import build_context
from ..rag.index import search

PROJECT_BRIEF = (
    "项目背景：聚乙烯吹塑包装膜降本。现用配方 LLDPE 60% + LDPE/HDPE/再生高压一级料 40%；现用 LLDPE 约 12,000 元/吨是成本大头，再生料约 8,000 元/吨。"
    "目标：四项性能（拉伸强度、撕裂强度、穿刺力、热封强度）全部 ≥ base 前提下降低成本。"
)

LIT_PROMPT = (
    "请根据下面这篇资料的内容生成一张“文献卡”，输出 JSON，字段："
    "citation(出处，含作者/期刊/年份/DOI), system(研究体系：材料、工艺、膜厚等), "
    "conclusions(与本项目有关的结论列表，每条注明是定性还是定量), data_points(可复用数据点列表，含数值与条件), "
    "credibility(可信度等级：3=期刊文献，写数字), relevance(与本项目的关系及可借鉴之处，1-3 句)。"
    "只写资料里明确写到的内容，没有的字段写“资料未提供”；不要编造数值。"
)

PAT_PROMPT = (
    "请根据下面这件专利的内容生成一张“专利卡”，输出 JSON，字段："
    "citation(公开号/标题/年份), applicant(申请人), formulation_range(权利要求中的配方范围：各组分及比例区间), "
    "claimed_effect(声称效果), borrow(我们能借鉴的点，列表), avoid(规避点：不能直接照抄的权利要求要点，列表), "
    "credibility(4)。只写专利里明确写到的内容，不要编造。"
)

MAT_PROMPT = (
    "请为材料【{name}】生成一张“材料卡”，只写与吹膜包装膜有关的内容。输出 JSON，字段："
    "name, grade_examples(牌号示例，列表), mfr, density, comonomer, role(在包装膜中的作用), "
    "effects(对象：拉伸/撕裂/穿刺/热封 四个键，值格式“方向(↑↑/↑/≈/↓/↓↓) + 幅度说明 + 依据[S#]”), "
    "typical_dosage(典型用量 %), price_estimate(估计价，元/吨；无依据写“待查”), risk(加工与性能风险), "
    "evidence(依据来源与可信度：引用 [S#]，没有出处的写“经验判断”)。"
    "数据来自 TDS 或文献的必须标 [S#]；不得编造牌号参数。"
)


def _source_text(source_id: int, max_chars: int = 14000) -> tuple[dict[str, Any], str]:
    src = db.q1("SELECT * FROM sources WHERE id=?", (source_id,))
    if not src:
        raise ValueError(f"资料 {source_id} 不存在")
    rows = db.q("SELECT text FROM chunks WHERE source_id=? ORDER BY ord", (source_id,))
    text = "\n".join(r["text"] for r in rows)
    if len(text) > max_chars:
        # 保留开头（摘要/引言）与结尾（结论）
        text = text[: int(max_chars * 0.7)] + "\n...\n" + text[-int(max_chars * 0.3):]
    return src, text


def make_literature_card(source_id: int) -> int:
    src, text = _source_text(source_id)
    prompt = PAT_PROMPT if src["source_type"] == "patent" else LIT_PROMPT
    card_type = "patent" if src["source_type"] == "patent" else "literature"
    meta = f"题名：{src['title']}\n作者：{src.get('authors') or ''}\n来源：{src.get('venue') or ''} {src.get('year') or ''}\nDOI/URL：{src.get('doi') or src.get('url') or ''}"
    data = llm.chat_json([
        {"role": "system", "content": PROJECT_BRIEF + " 你是文献摘要员，输出严格 JSON。"},
        {"role": "user", "content": f"{prompt}\n\n{meta}\n\n正文：\n{text}"},
    ], max_tokens=3000)
    if not isinstance(data, dict):
        raise llm.LLMError("卡片生成结果不是 JSON 对象")
    cred = int(data.get("credibility") or src.get("credibility") or 3) if str(data.get("credibility", "")).strip().isdigit() else int(src.get("credibility") or 3)
    return kb.save_card(card_type, src["title"][:80], data, source_id=source_id, credibility=cred)


def make_material_card(name: str, top_k: int = 10) -> tuple[int, list[dict[str, Any]]]:
    """基于知识库检索生成材料卡；返回 (card_id, citations)。"""
    hits = search(f"{name} 吹膜 包装膜 性能 热封 穿刺 撕裂 MFR 密度", top_k=top_k)
    context, citations = build_context(hits, max_chars=8000)
    data = llm.chat_json([
        {"role": "system", "content": PROJECT_BRIEF + " 你是聚乙烯薄膜材料学讲师，输出严格 JSON。"},
        {"role": "user", "content": MAT_PROMPT.format(name=name) + "\n\n可用资料片段：\n" + (context or "（知识库暂无资料，请写经验判断并标明）")},
    ], max_tokens=3000)
    if not isinstance(data, dict):
        raise llm.LLMError("卡片生成结果不是 JSON 对象")
    data.setdefault("name", name)
    if citations:
        data["sources"] = [f"{c['label']}: {c['cite']}" for c in citations]
    cred = 3 if context else 6
    return kb.save_card("material", name, data, credibility=cred), citations
