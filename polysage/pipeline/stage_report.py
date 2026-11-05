"""⑤ 报告与首轮实验意见：汇总 ①～④ 产出 → 首轮试验建议（LLM 选代表 + DOE 补点）→ 内部版 / 甲方版 Word。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from docx import Document
from docx.shared import Pt

from .. import db, kb, llm
from ..config import KB
from ..formulation import library as L
from ..formulation.materials import list_materials
from . import state, task

PICK_PROMPT = (
    "任务：{brief}\n\n下面是 ABA 三层膜的分档候选方案（含元/吨、相对现配方的纵横拉断力、透光档、依据、把握）。首轮小试只能安排 6～8 个配方，"
    "要求覆盖三个透光档和不同降本思路（B 层填充比例、厚度、再生比例、加新料），兼顾“降本大”和“把握高”。"
    "输出 JSON：{{\"picks\": [{{\"rank\": 排名, \"reason\": \"入选理由\", \"watch\": \"现场与检测要重点看的项\"}}], "
    "\"advice\": \"给甲方的首轮试验总体建议（工艺固定、留样、再生料同批次等，3～5 句）\"}}\n\nTop 20：\n{top}"
)


def _md_to_docx(doc: Document, md: str) -> None:
    table_buf: list[list[str]] = []

    def flush_table():
        nonlocal table_buf
        rows = [r for r in table_buf if not re.fullmatch(r"[\s|:\-]+", "".join(r))]
        if rows:
            tb = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            tb.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, c in enumerate(r):
                    tb.cell(i, j).text = c.strip()
                    for p in tb.cell(i, j).paragraphs:
                        for run in p.runs:
                            run.font.size = Pt(9)
        table_buf = []

    for line in md.splitlines():
        s = line.rstrip()
        if s.startswith("|"):
            table_buf.append([c for c in s.strip("|").split("|")])
            continue
        if table_buf:
            flush_table()
        if not s.strip():
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            level = min(len(m.group(1)), 3)
            doc.add_heading(m.group(2).strip(), level=level)
        elif re.match(r"^\s*[-*]\s+", s):
            doc.add_paragraph(re.sub(r"^\s*[-*]\s+", "", s), style="List Bullet")
        elif re.match(r"^\s*\d+[.、)]\s+", s):
            doc.add_paragraph(re.sub(r"^\s*\d+[.、)]\s+", "", s), style="List Number")
        else:
            doc.add_paragraph(s.replace("**", ""))
    if table_buf:
        flush_table()


def _df_table(doc: Document, df: pd.DataFrame, font: int = 8) -> None:
    if df.empty:
        doc.add_paragraph("（无）")
        return
    tb = doc.add_table(rows=len(df) + 1, cols=len(df.columns))
    tb.style = "Table Grid"
    for j, c in enumerate(df.columns):
        tb.cell(0, j).text = str(c)
    for i, (_, r) in enumerate(df.iterrows(), 1):
        for j, c in enumerate(df.columns):
            v = r[c]
            tb.cell(i, j).text = "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)
    for row in tb.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(font)


def pick_first_round(top: list[dict[str, Any]], brief: str) -> dict[str, Any]:
    listing = "\n".join(f"{r['rank']}. {r['formula']} | 成本 {r['cost']} | 降本 {r['savings_pct']}% | 预期 {r.get('expected')} | 风险 {r.get('risk')} | 把握 {r.get('confidence')}"
                        for r in top)
    try:
        data = llm.chat_json([{"role": "system", "content": "你是项目技术负责人，输出严格 JSON。"},
                              {"role": "user", "content": PICK_PROMPT.format(brief=brief, top=listing)}], max_tokens=1500, thinking=1024)
    except (llm.LLMNotConfigured, llm.LLMError) as e:
        # 模型没配或这次调用失败：不让整个报告阶段挂掉，按排名取前 6 并注明原因
        why = "LLM 未配置" if isinstance(e, llm.LLMNotConfigured) else f"模型调用失败：{str(e)[:80]}"
        state.log(f"首轮入选改为按排名取前 6（{why}）")
        picks = [{"rank": r["rank"], "reason": f"（{why}：按排名取前 6）", "watch": ""} for r in top[:6]]
        return {"picks": picks, "advice": "工艺固定、同批再生料、留样；模型恢复后重跑 ⑤ 可重新生成建议。"}
    return data if isinstance(data, dict) else {"picks": [], "advice": ""}


def build_docs(echo: Callable[[str], None] | None = None) -> list[Path]:
    t = task.load()
    brief = task.brief(t)
    design = state.stage("design")
    top = design.get("top", [])
    collect = state.stage("collect")
    mech_md = kb.read_text(KB["project"] / "现配方机理报告.md") or "（机理报告尚未生成）"
    mats = list_materials(active_only=False)
    picks = pick_first_round(top, brief) if top else {"picks": [], "advice": ""}
    # 首轮试验：ABA 版（现配方 3 个厚度对照 + D-最优）；已经出过就沿用
    from ..aba import experiments as AE
    from ..aba import rules as AR

    doe_path, n_trials = None, 0
    try:
        if not AE.trials():
            doe_path = AE.first_round()["path"]
        else:
            doe_path = str(AE.export_round(max(t["round"] for t in AE.trials())))
        n_trials = len(AE.trials())
    except Exception as e:  # noqa: BLE001
        state.log(f"首轮试验方案生成失败：{e}", echo)
    arrow = lambda v: "↑" if v > 0.02 else ("↓" if v < -0.02 else "≈")  # noqa: E731
    coef = AR.load_table()
    outputs = []
    for version in ("内部版", "甲方版"):
        doc = Document()
        doc.add_heading(f"包装膜配方降本 —— 发现阶段报告（{version}）", 0)
        doc.add_paragraph(f"生成时间：{db.now()}    工具：膜方 AI（DeepSeek-V4-Pro 驱动）")
        doc.add_heading("1 任务书", 1)
        doc.add_paragraph(brief)
        doc.add_heading("2 资料采集概况", 1)
        doc.add_paragraph(f"检索命中 {collect.get('n_hits', 0)} 条，去重 {collect.get('n_unique', 0)} 条，相关保留 {collect.get('n_kept', 0)} 条，"
                          f"入库 {collect.get('n_ingested', 0)} 条，生成卡片 {collect.get('n_cards', 0)} 张。来源清单见附件《来源清单.xlsx》。")
        st = db.q("SELECT source_type, provider, COUNT(*) n FROM sources GROUP BY source_type, provider ORDER BY n DESC")
        if st:
            _df_table(doc, pd.DataFrame(st).rename(columns={"source_type": "类型", "provider": "来源库", "n": "数量"}))
        doc.add_heading("3 现配方机理分析", 1)
        _md_to_docx(doc, re.sub(r"^# .*\n", "", mech_md))
        doc.add_heading("4 候选材料清单", 1)
        mdf = pd.DataFrame([{"代码": m["code"], "材料": m["name"], "作用": m.get("role"), "典型用量%": f"{m.get('typical_min')}～{m.get('typical_max')}",
                             "纵/横/透光": (lambda c: f"{arrow(c['md'])} {arrow(c['td'])} {arrow(c['trans'] / 5)}")(coef[AR.classify(m)]),
                             "放哪层": coef[AR.classify(m)]["layers"],
                             "参考价(元/吨)": (m.get("price_estimate") if version == "内部版" else ("实价" if m.get("price_actual") else "待询价")),
                             "风险": m.get("risk"), "用于配方": m.get("use_flag")} for m in mats])
        _df_table(doc, mdf)
        if version == "内部版":
            doc.add_paragraph("参考价为网查/假设值，仅用于排序；正式成本以甲方回填实价为准。")
        doc.add_heading("5 ABA 分档候选方案【定性预测】", 1)
        tdf = pd.DataFrame([{"序号": r["rank"], "方案（A 层 / B 层 / 厚度）": r["formula"],
                             "估算成本(元/吨)": (r["cost"] if version == "内部版" else "待核算"),
                             "预期影响说明": r.get("expected"), "风险点": r.get("risk"), "过关把握": r.get("confidence")} for r in top])
        _df_table(doc, tdf)
        doc.add_paragraph("纵向/横向拉断力为相对现配方的倍数、透光为 0 一般 / 1 好 / 2 很好，均为定性模型预测，以同工艺实测为准；"
                          "成本按吨，厚度不改变每吨价格。")
        doc.add_heading("6 首轮实验意见", 1)
        doc.add_paragraph(picks.get("advice", ""))
        pdf = pd.DataFrame([{"序号": p.get("rank"), "方案": next((r["formula"] for r in top if r["rank"] == p.get("rank")), ""),
                             "入选理由": p.get("reason"), "重点观察": p.get("watch")} for p in picks.get("picks", [])])
        _df_table(doc, pdf)
        if doe_path:
            doc.add_paragraph(f"用于校准模型的首轮试验见《{Path(doe_path).name}》（共 {n_trials} 组：现配方 3.5 / 4.5 / 5.5 丝对照 + "
                              "D-最优覆盖再生比例、B 层填充、B 层 HDPE、A 层占比、厚度）。回填后系数按数据校准，再推荐下一轮。")
        doc.add_heading("7 下一步", 1)
        for s in ["更新贸易商日报 / 正式报价 → 分档方案自动按新价格重算",
                  "做三块透光标样（一般 / 好 / 很好），统一判档口径",
                  "按首轮试验方案同机台同工艺打样，测纵横向拉断力（N）、实测厚度、透光档",
                  "在「实验与模型」上传回填表 → 校准系数 → 推荐下一轮"]:
            doc.add_paragraph(s, style="List Number")
        if version == "内部版":
            doc.add_heading("8 出处清单", 1)
            srcs = db.q("SELECT id, title, provider, year, doi, url FROM sources ORDER BY id")
            _df_table(doc, pd.DataFrame(srcs).rename(columns={"id": "#", "title": "题名", "provider": "来源库", "year": "年份", "doi": "DOI", "url": "链接"}), font=7)
        out = KB["project"] / f"发现阶段报告_{version}.docx"
        doc.save(out)
        outputs.append(out)
    return outputs


def _formulation_by_formula(formula: str) -> dict[str, Any] | None:
    for f in L.list_formulations():
        if f["formula"] == formula:
            return f
    return None


def run(echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    with state.running("report", echo):
        outs = build_docs(echo)
        summary = {"outputs": [str(p) for p in outs]}
        state.set_stage("report", **summary)
    return summary
