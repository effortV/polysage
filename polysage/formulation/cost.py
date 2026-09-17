"""成本核算：元/吨（估计价 / 实价两档）、共混密度、面积成本指数。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import constraints as C
from .materials import list_materials


@dataclass
class CostResult:
    cost_estimate: float | None
    cost_actual: float | None          # 全部组分都有实价时才给出
    cost_used: float | None            # 有实价用实价、否则估计价的混合口径
    density: float | None
    area_index: float | None           # 相对 base（100）
    missing_estimate: list[str] = field(default_factory=list)
    missing_actual: list[str] = field(default_factory=list)
    breakdown: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tier(self) -> str:
        """价格口径：实价（全部实价）/ 混合（部分实价）/ 估计价 / 缺。"""
        if self.cost_actual is not None:
            return "实价"
        if self.cost_used is None:
            return "缺"
        n_actual = sum(1 for b in self.breakdown if b.get("price_actual") is not None)
        return "混合" if n_actual else "估计价"

    @property
    def cost_display(self) -> str:
        if self.cost_actual is not None:
            return f"{self.cost_actual:,.0f}（实价）"
        if self.cost_used is not None:
            tier = "混合口径" if self.cost_used != self.cost_estimate else "估计价"
            return f"{self.cost_used:,.0f}（{tier}）"
        return "待核算"


def _materials_map() -> dict[str, dict[str, Any]]:
    return {m["code"]: m for m in list_materials(active_only=False)}


def blend_density(components: dict[str, float], mats: dict[str, dict[str, Any]] | None = None) -> float | None:
    mats = mats or _materials_map()
    inv = 0.0
    total = 0.0
    for k, w in components.items():
        if not w:
            continue
        rho = (mats.get(k) or {}).get("density")
        if not rho:
            return None
        inv += (w / 100.0) / float(rho)
        total += w / 100.0
    return (total / inv) if inv > 0 else None


def compute(components: dict[str, float], base: dict[str, float] | None = None,
            mats: dict[str, dict[str, Any]] | None = None) -> CostResult:
    mats = mats or _materials_map()
    est = act = used = 0.0
    miss_e: list[str] = []
    miss_a: list[str] = []
    breakdown = []
    for k, w in components.items():
        if not w:
            continue
        m = mats.get(k)
        pe = m.get("price_estimate") if m else None
        pa = m.get("price_actual") if m else None
        if pe is None:
            miss_e.append(k)
        else:
            est += w / 100.0 * pe
        if pa is None:
            miss_a.append(k)
        else:
            act += w / 100.0 * pa
        p_used = pa if pa is not None else pe
        if p_used is not None:
            used += w / 100.0 * p_used
        breakdown.append({"code": k, "name": (m or {}).get("name", k), "pct": w, "price_estimate": pe,
                          "price_actual": pa, "price_used": p_used, "tier": "实价" if pa is not None else "估计价",
                          "contribution": (w / 100.0 * p_used) if p_used is not None else None})
    cost_e = est if not miss_e else None
    cost_a = act if not miss_a else None
    cost_u = used if not (miss_e and miss_a) and not (set(miss_e) & set(miss_a)) else None
    rho = blend_density(components, mats)
    area = None
    base = base or C.base_formulation()
    if rho and cost_u is not None and base:
        b_rho = blend_density(base, mats)
        b = compute_simple(base, mats)
        if b_rho and b:
            area = (cost_u * rho) / (b * b_rho) * 100.0
    return CostResult(cost_e, cost_a, cost_u, rho, area, miss_e, miss_a, breakdown)


def compute_simple(components: dict[str, float], mats: dict[str, dict[str, Any]] | None = None) -> float | None:
    """混合口径成本（不递归计算面积指数）。"""
    mats = mats or _materials_map()
    total = 0.0
    for k, w in components.items():
        if not w:
            continue
        m = mats.get(k) or {}
        p = m.get("price_actual") if m.get("price_actual") is not None else m.get("price_estimate")
        if p is None:
            return None
        total += w / 100.0 * p
    return total


def savings_vs_base(cost: float | None, base: dict[str, float] | None = None) -> tuple[float | None, float | None]:
    base = base or C.base_formulation()
    b = compute_simple(base)
    if cost is None or b is None:
        return None, None
    return b - cost, (b - cost) / b * 100.0


CODE_ORDER = ["LL", "LLC", "LL6", "mLL", "mLL8", "LD", "HD", "MD", "R1", "R2", "RL", "PCR", "RHD", "SC", "POE", "POP", "VL",
              "OBC", "EVA", "EMA", "ION", "FL", "TFL", "TALC", "AD", "CP", "NUC", "ADR"]


def ordered(components: dict[str, float]) -> dict[str, float]:
    """按固定代码顺序排列组分（显示与建模用）。"""
    rank = {k: i for i, k in enumerate(CODE_ORDER)}
    return {k: components[k] for k in sorted(components, key=lambda x: (rank.get(x, 999), x)) if components[k]}


def format_components(components: dict[str, float]) -> str:
    return " / ".join(f"{k} {v:g}" for k, v in ordered(components).items())
