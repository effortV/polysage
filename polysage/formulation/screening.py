"""替代品窗口筛选器：按“性能相近（密度 / 熔点 / MFR / 共聚单体）且价格更低”从原料库筛候选。

以现用材料（默认 LL）的物性为基准生成窗口；缺失的物性不排除、只标“未知”，由后续 TDS 补齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .materials import list_materials

# 替代功能分组：候选材料 → 它替代的是现配方中的哪个功能
FUNCTION_GROUPS: dict[str, tuple[str, list[str]]] = {
    "同类更便宜的 LLDPE": ("直接替代现用 LLDPE", ["LLC", "LL6"]),
    "高效韧性树脂（低比例替代）": ("用 10～15% 顶替 25～30 个点现用 LLDPE 的韧性", ["mLL", "mLL8", "POE", "POP", "VL"]),
    "HDPE 的替代 / 去除": ("挺度来源改为 MDPE 或直接去掉 HDPE", ["MD"]),
    "再生料（成本填充）": ("提高再生比例、用再生线性料补韧性", ["R1", "RL", "R2", "PCR", "SC"]),
    "热封改性": ("补偿再生料带来的热封下降", ["EVA", "EMA"]),
    "助剂（必配 / 条件）": ("PPA + 抗氧；相容剂；扩链剂", ["AD", "CP", "ADR", "NUC"]),
}

DEFAULT_WINDOWS = {
    "density": 0.006,        # ± g/cm³
    "melting_point": 6.0,    # ± ℃
    "mfr_ratio": 2.5,        # 参考值 ÷ ratio ～ × ratio
}


@dataclass
class ScreenResult:
    code: str
    name: str
    group: str
    role: str
    price: float | None
    price_tier: str
    price_delta: float | None          # 相对基准材料（负 = 更便宜）
    density: float | None
    melting_point: float | None
    mfi: float | None
    comonomer: str
    fits: dict[str, str] = field(default_factory=dict)   # 维度 → 在窗口内 / 超窗口 / 未知
    fit_score: float = 0.0             # 0～1，已知维度中在窗口内的比例
    cheaper: bool = False
    effects: dict[str, str] = field(default_factory=dict)
    risk: str = ""
    use_flag: str = ""
    typical: str = ""
    verdict: str = ""                  # 直接替代候选 / 低比例改性候选 / 成本填充 / 不建议


def windows_from_reference(ref: dict[str, Any], tol: dict[str, float] | None = None) -> dict[str, tuple[float, float] | None]:
    tol = {**DEFAULT_WINDOWS, **(tol or {})}
    w: dict[str, tuple[float, float] | None] = {}
    d, mp, mfr = ref.get("density"), ref.get("melting_point"), ref.get("mfi")
    w["density"] = (d - tol["density"], d + tol["density"]) if d else None
    w["melting_point"] = (mp - tol["melting_point"], mp + tol["melting_point"]) if mp else None
    w["mfr"] = (mfr / tol["mfr_ratio"], mfr * tol["mfr_ratio"]) if mfr else None
    return w


def _fit(value: float | None, window: tuple[float, float] | None) -> str:
    if value is None or window is None:
        return "未知"
    return "在窗口内" if window[0] - 1e-9 <= value <= window[1] + 1e-9 else "超窗口"


def screen(reference: str = "LL", *, price_max: float | None = None, tol: dict[str, float] | None = None,
           include_reference: bool = False) -> dict[str, Any]:
    """返回 {windows, reference, groups: {组名: [ScreenResult...]}, flat: [...]}，组内按价格升序。"""
    mats = {m["code"]: m for m in list_materials(active_only=True)}
    ref = mats.get(reference)
    if not ref:
        raise ValueError(f"基准材料 {reference} 不在原料库")
    windows = windows_from_reference(ref, tol)
    ref_price = ref.get("price")
    limit = price_max if price_max is not None else ref_price
    groups: dict[str, list[ScreenResult]] = {}
    flat: list[ScreenResult] = []
    for gname, (role, codes) in FUNCTION_GROUPS.items():
        for code in codes:
            m = mats.get(code)
            if not m or (code == reference and not include_reference):
                continue
            fits = {"密度": _fit(m.get("density"), windows["density"]), "熔点": _fit(m.get("melting_point"), windows["melting_point"]),
                    "MFR": _fit(m.get("mfi"), windows["mfr"])}
            known = [v for v in fits.values() if v != "未知"]
            score = (sum(1 for v in known if v == "在窗口内") / len(known)) if known else 0.0
            price = m.get("price")
            cheaper = bool(price is not None and limit is not None and price < limit)
            delta = (price - ref_price) if (price is not None and ref_price is not None) else None
            if gname == "同类更便宜的 LLDPE":
                verdict = "直接替代候选" if (cheaper and score >= 0.67) else ("价格不占优" if not cheaper else "物性需核对")
            elif gname == "高效韧性树脂（低比例替代）":
                verdict = "低比例改性候选（单价高、用量少）"
            elif gname == "再生料（成本填充）":
                verdict = "成本填充候选（比例上限靠实验）" if cheaper else "价格不占优"
            elif gname == "HDPE 的替代 / 去除":
                verdict = "挺度替代候选"
            else:
                verdict = "配套"
            if str(m.get("use_flag", "")).startswith("否"):
                verdict = "不建议：" + str(m.get("use_flag"))
            r = ScreenResult(code=code, name=m["name"], group=gname, role=role, price=price, price_tier=m.get("price_tier", ""),
                             price_delta=delta, density=m.get("density"), melting_point=m.get("melting_point"), mfi=m.get("mfi"),
                             comonomer=m.get("comonomer") or "", fits=fits, fit_score=round(score, 2), cheaper=cheaper,
                             effects=m.get("effects") or {}, risk=m.get("risk") or "", use_flag=m.get("use_flag") or "",
                             typical=f"{m.get('typical_min')}～{m.get('typical_max')}%", verdict=verdict)
            groups.setdefault(gname, []).append(r)
            flat.append(r)
    for lst in groups.values():
        lst.sort(key=lambda r: (r.price is None, r.price or 0))
    return {"reference": {"code": reference, "name": ref["name"], "price": ref_price, "density": ref.get("density"),
                          "melting_point": ref.get("melting_point"), "mfi": ref.get("mfi"), "comonomer": ref.get("comonomer")},
            "windows": windows, "price_max": limit, "groups": groups, "flat": flat}


def to_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for r in res["flat"]:
        rows.append({"分组": r.group, "代码": r.code, "材料": r.name, "价格(元/吨)": r.price, "价格口径": r.price_tier,
                     "较基准": round(r.price_delta) if r.price_delta is not None else None, "更便宜": "是" if r.cheaper else "否",
                     "密度": r.density, "熔点": r.melting_point, "MFR": r.mfi, "共聚单体": r.comonomer,
                     "密度窗口": r.fits["密度"], "熔点窗口": r.fits["熔点"], "MFR窗口": r.fits["MFR"], "物性匹配度": r.fit_score,
                     "典型用量": r.typical, "拉/撕/穿/封": " ".join(str(r.effects.get(k, "?"))[:3] for k in ("拉伸", "撕裂", "穿刺", "热封")),
                     "结论": r.verdict, "风险": r.risk})
    return rows
