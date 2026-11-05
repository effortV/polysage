"""② 现配方机理分析：回答“为什么是 LLDPE 60 / LDPE+HDPE+再生 40”，每个结论带 [S#]，缺依据自动补检索。"""
from __future__ import annotations

import re
from typing import Any, Callable

from .. import kb, llm
from ..config import KB
from ..ingest import ingest_hit
from ..rag.answer import build_context
from ..rag.index import search
from ..sources import search_all
from . import state, task

COMPONENTS = {
    "LL": "LLDPE（线型低密度聚乙烯，C4 丁烯共聚）",
    "LD": "LDPE（高压低密度聚乙烯）",
    "HD": "HDPE（高密度聚乙烯）",
    "R1": "再生高压一级料（回收 LDPE/LLDPE 透明料）",
}

QUESTIONS: list[tuple[str, str, str]] = [
    ("LL", "LLDPE 在 ABA 吹膜中的作用", "LLDPE 对薄膜纵向、横向拉伸强度（拉断力）和透明度/雾度各有什么影响？为什么它是 A、B 两层的主料？"),
    ("LD", "LDPE 在共混中的作用", "在 LLDPE 中加入 LDPE 对膜泡稳定性、加工性、透明度（雾度、光泽）以及纵横向拉伸有何影响？更适合放表层还是芯层？典型加入量？"),
    ("HD", "HDPE 加在芯层的作用", "芯层加入约 10% HDPE 对挺度、纵向/横向拉伸强度、雾度的影响？为什么横向容易劈裂、用量通常不超过 10%？"),
    ("R1", "再生料对薄膜性能的影响", "高压再生回料（接近新料颜色）加入 A、B 两层后对纵横向拉伸、透明度、晶点的影响；比例上限受什么限制；表层和芯层能放多少？"),
    ("FL", "碳酸钙填充母料在芯层的作用", "PE 吹膜芯层加 10%~50% 碳酸钙填充母料，对纵向/横向拉伸、透明度、密度和克重的影响；填充上限受什么限制？有没有透光损失更小的填充料？"),
    ("ALL", "ABA 结构为什么这样设计", "A 层（两面表层）约 45%、B 层（芯层）约 55%、填充只进芯层这类 ABA 结构的设计逻辑：表层负责什么、芯层负责什么；"
     "改变 A/B 比例、厚度对拉断力和透明度的影响；降本空间在哪里？"),
    ("PROC", "工艺与厚度的影响", "吹胀比、霜线高度、牵引比对纵横向拉伸各向异性的影响；膜厚对拉断力（N）和透明度的影响；比较配方时为什么必须固定工艺？"),
]

REPORT_PROMPT = (
    "任务：{brief}\n\n下面是分问题得到的带引用的分析结果（引用编号 [S#] 与最后的出处表对应）。"
    "请整理成《现配方机理报告》，Markdown，固定结构：\n"
    "# 现配方机理报告\n## 1 各组分的作用（表：组分 | 放哪层 | 用量 | 对纵向拉伸 | 对横向拉伸 | 对透光 | 其他作用 | 依据）\n"
    "## 2 为什么是这个比例（每条结论后标 [S#]）\n## 3 性能担当与成本担当\n## 4 替代空间（按功能列：强度来源 / 透光来源 / 挺度来源 / 降本填充 / 助剂，各写放哪层、可能的替代方向与要守住的约束）\n"
    "## 5 待验证事项（没有文献依据、需实验确认的点）\n"
    "规则：只用给出的资料；每个定性结论必须带 [S#]；没有依据的写“待验证”；不要编造数值。\n\n分析结果：\n{analyses}"
)


def _answer(question: str, top_k: int = 8) -> tuple[str, list[dict[str, Any]]]:
    hits = search(question, top_k=top_k)
    ctx, cits = build_context(hits, max_chars=7000)
    if not ctx:
        return "", []
    res = llm.chat([
        {"role": "system", "content": "你是聚乙烯薄膜材料学讲师。只依据资料片段回答，每个结论后标 [S#]；资料不足的部分明确写“资料不足”。中文，条列式。"},
        {"role": "user", "content": f"资料片段：\n{ctx}\n\n问题：{question}"},
    ], temperature=0.2, max_tokens=1500, thinking=1024)
    return res.content, cits


def _supplement(question: str, echo: Callable[[str], None] | None) -> int:
    """资料不足时补检索并入库（英文检索式由 LLM 生成）。"""
    try:
        data = llm.chat_json([{"role": "system", "content": "输出严格 JSON。"},
                              {"role": "user", "content": f"为下面的问题生成 2 条英文学术检索式（关键词形式）：{question}\n输出 {{\"queries\": [..]}}"}],
                             max_tokens=200)
        queries = [q for q in data.get("queries", []) if isinstance(q, str)][:2]
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for q in queries:
        hits, _ = search_all(q, providers=["openalex", "crossref"], limit=6)
        for h in hits[:6]:
            try:
                ingest_hit(h, fetch_fulltext=True)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        state.log(f"补检索：{q} → 入库 {len(hits[:6])}", echo)
    return n


def run(echo: Callable[[str], None] | None = None, supplement: bool = True) -> dict[str, Any]:
    t = task.load()
    brief = task.brief(t)
    analyses = []
    all_cits: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    with state.running("mechanism", echo):
        for code, title, q in QUESTIONS:
            ans, cits = _answer(q)
            if (not ans or "资料不足" in ans) and supplement:
                state.log(f"[{title}] 资料不足，补检索", echo)
                if _supplement(q, echo):
                    ans, cits = _answer(q)
            if not ans:
                gaps.append(title)
                ans = "（知识库无相关资料，待补充文献/手动上传）"
            # 重新编号引用：S# 在全局唯一
            mapping = {}
            for c in cits:
                key = str(c["source_id"])
                if key not in all_cits:
                    all_cits[key] = {**c, "label": f"S{len(all_cits) + 1}"}
                mapping[c["label"]] = all_cits[key]["label"]
            ans = re.sub(r"\[S(\d+)\]", lambda m: f"[{mapping.get('S' + m.group(1), 'S' + m.group(1))}]", ans)
            analyses.append(f"### {title}\n{ans}")
            state.log(f"[{title}] 完成，引用 {len(cits)} 条", echo)
        legend = "\n".join(f"- [{c['label']}] {c['cite']}" for c in all_cits.values())
        res = llm.chat([{"role": "system", "content": "你是项目技术负责人，写报告。"},
                        {"role": "user", "content": REPORT_PROMPT.format(brief=brief, analyses="\n\n".join(analyses))}],
                       temperature=0.2, max_tokens=5000, thinking=4096)
        report = res.content.strip() + "\n\n## 出处\n" + legend + "\n"
        # 校验：第 2 节每个段落至少一个引用，否则标注
        uncited = [ln for ln in report.splitlines() if ln.strip().startswith(("- ", "* ", "1.", "2.", "3.")) and "[S" not in ln and "待验证" not in ln]
        path = KB["project"] / "现配方机理报告.md"
        kb.write_text(path, report)
        summary = {"n_citations": len(all_cits), "gaps": gaps, "n_uncited_lines": len(uncited), "outputs": [str(path)]}
        state.set_stage("mechanism", **summary)
    return summary
