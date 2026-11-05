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
