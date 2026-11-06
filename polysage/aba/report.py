"""分档结果的导出、入配方库、采购清单。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import KB
from . import optimize as O
from . import rules as R
from .model import Design


def export_xlsx(out: dict[str, Any], path: Path | None = None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M")
    path = path or (KB["formulation"] / f"ABA分档方案_{stamp}.xlsx")
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in O.rows(out)]
    ref = out["ref"]
    used = sorted({k for r in O.rows(out) for k in r["_ev"]["design"].whole()} | set(ref["design"].whole()))
    from .prices import build

    scen = build(out["scenario"]["name"], out["query"].get("custom_pct", 0.0))
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="分档方案", index=False)
        pd.DataFrame([{"现配方": ref["text"], "元/吨": ref["cost"], "克重 g/m²": ref["gsm"], "透光": ref["grade_label"]}]).to_excel(
            xw, sheet_name="现配方", index=False)
        pd.DataFrame([{"代码": c, "单价(元/吨)": scen["prices"].get(c)} for c in used]).to_excel(xw, sheet_name="价格", index=False)
        notes = [f"价格场景：{out['scenario']['label']}"] + out["notes"] + [
            "纵向/横向拉断力是相对现配方（同配方、对照厚度）的倍数；实验回填校准后会给出 N 的绝对值。",
            "透光：0 一般 / 1 好 / 2 很好，由定性模型打分预测，需按标样目视验证。"]
        pd.DataFrame({"说明": notes}).to_excel(xw, sheet_name="说明", index=False)
    return path


def effects_short(ev: dict[str, Any]) -> str:
    return f"纵 {ev['md_rel']:.2f}× 横 {ev['td_rel']:.2f}× 透光{ev['grade_label']}"


def save_to_library(out: dict[str, Any], origin: str = "分档寻优", per_grade: int = 1) -> list[str]:
    """每组每档的前 per_grade 个方案入配方库（结构 ABA，组分存整膜，按层的配方放在 predicted 里）。"""
    from ..formulation import library as L

    codes = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    for gname, per in out["groups"].items():
        for g, items in per.items():
            for i, ev in enumerate(items[:per_grade], 1):
                d: Design = ev["design"]
                whole = d.whole()
                exist = L.find_by_components(whole)
                code = exist["code"] if exist and exist.get("structure") == "ABA" else L.next_code("H")
                try:
                    L.save_formulation(code, whole, structure="ABA",
                                       predicted={"aba": d.to_dict(), "aba_text": d.text(), "md_rel": ev["md_rel"],
                                                  "td_rel": ev["td_rel"], "grade": ev["grade"], "grade_label": ev["grade_label"],
                                                  "effects_short": effects_short(ev), "pass_confidence": ev["confidence"],
                                                  "scenario": out["scenario"]["label"]},
                                       rationale="；".join(ev.get("drivers") or []),
                                       risks=f"把握 {ev['confidence']}；定性预测，需小试验证",
                                       priority=f"{gname} 透光≥{R.GRADE_LABEL[g]} #{i}", status="候选",
                                       origin=f"{origin} {stamp}")
                    codes.append(code)
                except Exception:  # noqa: BLE001  买不到的料不入库（save_formulation 会拒绝）
                    continue
    return codes


def procurement_for(ev: dict[str, Any], tons: float = 1.0) -> dict[str, Any]:
    from .. import procurement

    d: Design = ev["design"]
    return procurement.plan(d.whole(), tons, name=d.text())


def export_docx(out: dict[str, Any], path: Path | None = None) -> Path:
    """分档结果 → Word：要求、现配方、每档最省的几个方案（含为什么这样配）、说明。"""
    from .. import wordout as W
    from .model import layer_text

    stamp = datetime.now().strftime("%Y-%m-%dT%H%M")
    path = path or (KB["formulation"] / f"ABA分档方案_{stamp}.docx")
    q, ref = out["query"], out["ref"]
    req = [f"{label}拉断力 ≥ 现配方 × {q[k]:g}" for k, label in (("md_req", "纵向"), ("td_req", "横向")) if q.get(k) is not None]
    doc = W.new_doc("ABA 三层膜 · 分档最低成本方案", f"价格：{out['scenario']['label']}")
    doc.add_heading("要求与对照", level=1)
    W.bullets(doc, [f"要求：{'，'.join(req) or '拉断力不限'}；厚度 {q['t_min']:g}~{q['t_max']:g} 丝；"
                    f"A 层占比 {q['a_min']:.0%}~{q['a_max']:.0%}；B 层填充 ≤{q['filler_max']:g}%，HDPE ≤{q['hdpe_max']:g}%。",
                    f"现配方（{q['t_ref']:g} 丝）：{ref['text']}，{ref['cost'] or '缺价'} 元/吨，克重 {ref['gsm']:.0f} g/m²，"
                    f"每平方米 {ref.get('cost_m2') or 0:.3f} 元，透光{ref['grade_label']}。"])
    for gname, per in out["groups"].items():
        doc.add_heading("只用现有料" if gname == "现有料" else "加 / 换新料（只列比现有料最优更省的）", level=1)
        lines = []
        for g, items in per.items():
            for i, ev in enumerate(items, 1):
                d: Design = ev["design"]
                lines.append({"透光要求": f"≥{R.GRADE_LABEL[g]}", "#": i, "A 层": f"{d.a_ratio:.0%}：{layer_text(d.a)}",
                              "B 层": f"{1 - d.a_ratio:.0%}：{layer_text(d.b)}", "厚度": d.thickness, "元/吨": ev["cost"],
                              "较现配方": O.saving_text(ev.get("saving_pct")), "纵/横": f"{ev['md_rel']:.2f}/{ev['td_rel']:.2f}",
                              "透光": ev["grade_label"], "元/m²": ev.get("cost_m2"), "把握": ev["confidence"]})
        if not lines:
            W.para(doc, "没有（在约束内找不到，或不比只用现有料省）。")
            continue
        W.add_table(doc, lines, widths_cm=[1.5, 0.6, 3.3, 4.2, 1.0, 1.3, 1.7, 1.6, 1.0, 1.2, 1.0], font_size=8)
        for g, items in per.items():
            if items and items[0].get("drivers"):
                W.para(doc, f"**透光≥{R.GRADE_LABEL[g]} 第 1 名为什么这样配**：")
                W.bullets(doc, items[0]["drivers"][:4])
    doc.add_heading("说明", level=1)
    W.bullets(doc, list(out["notes"]) + [
        "纵向/横向拉断力是相对现配方（同配方、对照厚度）的倍数；实验回填校准后会给出 N 的绝对值。",
        "透光：0 一般 / 1 好 / 2 很好，由定性模型打分预测，需按标样目视验证。"])
    return W.save(doc, path)
