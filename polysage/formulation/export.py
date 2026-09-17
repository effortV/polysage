"""导出：Top 20 候选配方表（【初步预计】格式）与给甲方的询价清单（附录 E 格式）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..config import KB
from .materials import list_materials

# 询价清单模板（内部技术路线附录 E）
INQUIRY_SPECS: dict[str, dict[str, str]] = {
    "LLC": {"规格要求": "C4 LLDPE 吹膜级，MFR 1.5～2.5，密度 0.916～0.920（含副牌/宽规格料报价）", "参考牌号": "7042 / 218W / DFDA-7042", "用途": "直接替代现用 LLDPE"},
    "LL": {"规格要求": "现用牌号", "参考牌号": "现用", "用途": "基准价格（含税到厂）"},
    "mLL": {"规格要求": "MFR 0.8～1.2，密度 0.916～0.920，己烯共聚，吹膜级", "参考牌号": "国产茂金属 / 1018、2045 类", "用途": "少量替代 LLDPE，提高韧性"},
    "mLL8": {"规格要求": "MFR 0.8～1.2，密度 0.916 左右，辛烯共聚", "参考牌号": "Elite 5400 类", "用途": "同上，高韧性版本"},
    "LL6": {"规格要求": "MFR 1.0 左右，己烯共聚，吹膜级", "参考牌号": "按供应商", "用途": "LLDPE 的低价升级"},
    "RL": {"规格要求": "工业膜/边角料来源，无 PIB，MFR 1～3，灰分 ≤ 0.3%", "参考牌号": "—", "用途": "补充韧性的再生料"},
    "R2": {"规格要求": "MFR 1～3，灰分 ≤ 0.5%，过滤 ≥ 80 目", "参考牌号": "—", "用途": "三层芯层用"},
    "POE": {"规格要求": "乙烯-辛烯，密度 0.87～0.88，MFR 1～5", "参考牌号": "按供应商", "用途": "少量增韧"},
    "EVA": {"规格要求": "VA 5～9%，MFR 1～3，吹膜级", "参考牌号": "按供应商", "用途": "改善热封"},
    "AD": {"规格要求": "PPA + 抗氧复合，LLDPE 载体", "参考牌号": "按供应商", "用途": "加工助剂"},
    "TFL": {"规格要求": "细粒径 CaCO₃ 或 BaSO₄，含量 ≥ 70%", "参考牌号": "按供应商", "用途": "降本（默认不用，改变外观）"},
    "FL": {"规格要求": "CaCO₃ 含量 80%，粒径 1250～2500 目", "参考牌号": "按供应商", "用途": "降本（默认不用，改变外观）"},
    "MD": {"规格要求": "MDPE 吹膜级，密度 0.93～0.94", "参考牌号": "按供应商", "用途": "挺度与成本折中"},
    "PCR": {"规格要求": "混合 PE 消费后回料造粒，MFR 0.5～2", "参考牌号": "—", "用途": "最低成本料（默认不用，气味/颜色）"},
    "CP": {"规格要求": "MAH 接枝 PE，接枝率 ≥ 0.8%", "参考牌号": "按供应商", "用途": "再生料相容"},
}


def top20_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    data = []
    for r in rows:
        data.append({
            "排名": r.get("rank"), "设计思路": r.get("theme"), "配方（组分+比例%）": r.get("formula"),
            "估算成本(元/吨)": r.get("cost"), "价格口径": r.get("cost_tier"),
            "较 base 降本(元/吨)": round(r["savings"]) if r.get("savings") is not None else None,
            "降本%": round(r["savings_pct"], 1) if r.get("savings_pct") is not None else None,
            "面积成本指数": r.get("area_index"), "四项预期(拉 撕 穿 封)": r.get("effects_short"),
            "预期影响说明": r.get("expected"), "风险点": r.get("risks"), "风险等级": r.get("risk_level"),
            "过关把握": r.get("pass_confidence"), "依据": r.get("evidence"),
        })
    return pd.DataFrame(data)


def export_top20(rows: list[dict[str, Any]], path: Path | None = None, title: str = "Top 20 候选配方【初步预计】") -> Path:
    path = path or KB["formulation"] / "Top20候选配方_初步预计.xlsx"
    df = top20_dataframe(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Top20", index=False, startrow=2)
        ws = xw.sheets["Top20"]
        ws["A1"] = title
        ws["A2"] = "说明：四项性能为定性预期（↑↑/↑/≈/↓/↓↓），精确值以实测为准；成本按价格卡（估计价/实价）核算，标注口径。"
        for col, width in zip("ABCDEFGHIJKLMNO", [6, 12, 48, 14, 10, 14, 8, 12, 20, 40, 40, 8, 8, 8, 40]):
            ws.column_dimensions[col].width = width
    return path


def inquiry_list(rows: list[dict[str, Any]], path: Path | None = None) -> Path:
    """从候选配方提取出现过的材料，生成询价清单（甲方回填到厂含税价/供应商/起订量/报价日期）。"""
    path = path or KB["price"] / "询价清单.xlsx"
    codes: list[str] = []
    for r in rows:
        for k in (r.get("components") or {}):
            if k not in codes:
                codes.append(k)
    mats = {m["code"]: m for m in list_materials(active_only=False)}
    data = []
    for k in codes:
        m = mats.get(k, {})
        spec = INQUIRY_SPECS.get(k, {})
        data.append({
            "代码": k, "材料": m.get("name", k), "规格要求": spec.get("规格要求", f"MFR {m.get('mfi') or '—'}，密度 {m.get('density') or '—'}"),
            "参考牌号": spec.get("参考牌号", m.get("grade", "")), "用途（给采购看的一句话）": spec.get("用途", m.get("role", "")),
            "网查估计价(元/吨)": m.get("price_estimate"), "到厂含税价": "", "供应商": "", "起订量": "", "报价日期": "",
        })
    df = pd.DataFrame(data)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="询价清单", index=False)
        ws = xw.sheets["询价清单"]
        for col, width in zip("ABCDEFGHIJ", [8, 30, 44, 24, 30, 14, 12, 16, 10, 12]):
            ws.column_dimensions[col].width = width
    return path
