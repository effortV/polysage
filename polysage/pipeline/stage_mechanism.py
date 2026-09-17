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
    ("LL", "LLDPE 在吹塑包装膜中的作用", "LLDPE 对薄膜拉伸强度、撕裂强度、穿刺/落镖、热封性能各有什么影响？为什么它是主料？用量高低的物理原因是什么？"),
    ("LD", "LDPE 在共混中的作用", "在 LLDPE 中加入 LDPE 对膜泡稳定性、加工性、热封以及拉伸/撕裂/穿刺有何影响？典型加入量与上限是什么？"),
    ("HD", "HDPE 在共混中的作用", "少量 HDPE 加入 LLDPE/LDPE 膜对挺度、拉伸强度、撕裂（MD/TD）、热封的影响？为什么用量通常很低？"),
    ("R1", "再生料对薄膜性能的影响", "再生聚乙烯（一级透明回料）加入吹膜配方后对力学性能、凝胶/晶点、热封的影响；比例上限受什么限制；如何用抗氧剂/加工助剂/相容剂补偿？"),
    ("ALL", "为什么是这个比例", "LLDPE 60% + LDPE 10% + HDPE 5% + 再生料 25% 这类配方的设计逻辑是什么：谁提供韧性、谁稳泡、谁提挺度、谁降成本；各自的边际收益与上限；替代空间在哪里？"),
    ("PROC", "工艺对性能的影响", "吹胀比、霜线高度、模口间隙、熔温对 LLDPE 类薄膜撕裂各向异性、穿刺的影响，比较配方时为什么必须固定工艺？"),
]

REPORT_PROMPT = (
    "任务：{brief}\n\n下面是分问题得到的带引用的分析结果（引用编号 [S#] 与最后的出处表对应）。"
    "请整理成《现配方机理报告》，Markdown，固定结构：\n"
    "# 现配方机理报告\n## 1 各组分的作用（表：组分 | 用量 | 对拉伸 | 对撕裂 | 对穿刺 | 对热封 | 其他作用 | 依据）\n"
    "## 2 为什么是这个比例（每条结论后标 [S#]）\n## 3 性能担当与成本担当\n## 4 替代空间（按功能列：韧性来源 / 热封来源 / 挺度来源 / 成本填充 / 助剂，各写可能的替代方向与要守住的约束）\n"
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
