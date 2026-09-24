"""配方约束（内部技术路线表 4-2）：从 knowledge/00_项目/constraints.yaml 读取并校验。"""
from __future__ import annotations

from typing import Any

from .. import kb

DEFAULT_CONSTRAINTS: dict[str, Any] = {
    "base_formulation": {"LL": 60, "LD": 10, "HD": 5, "R1": 25},
    "base_note": "40% 内部比例（LD/HD/R1）为假设值，需在 Step 0 前与甲方确认",
    "cost_limit": "auto",      # auto = 现配方（base）按当前价格卡的成本；候选必须低于它
    "cost_target": "auto",     # auto = base 成本 × 0.95
    "groups": {
        "virgin_pe": ["LL", "LLC", "LL6", "mLL", "mLL8"],
        "ldpe_class": ["LD", "R1", "R2"],
        "hdpe": ["HD", "RHD"],
        "recycled": ["R1", "R2", "RL", "PCR", "RHD", "SC"],
        "secondary_recycled": ["R2"],
        "poe": ["POE", "POP", "VL", "OBC"],
        "eva": ["EVA", "EMA"],
        "filler": ["FL", "TFL", "TALC"],
        "additive": ["AD"],
    },
    "rules": [
        {"name": "新料 PE 合计（C4+C6+茂金属）", "group": "virgin_pe", "min": 20, "max": 60, "reason": "低于 20% 韧性无法保证"},
        {"name": "LDPE 类合计（LDPE 新料+再生高压料）", "group": "ldpe_class", "min": 35, "reason": "茂金属比例高时膜泡稳定性依赖 LDPE 类"},
        {"name": "HDPE", "group": "hdpe", "max": 5, "reason": "撕裂红线；用量高会发白"},
        {"name": "再生料合计（单层）", "group": "recycled", "max": 65, "applies": "mono", "reason": "单层膜再生料上限（外观与凝胶）"},
        {"name": "再生料合计（芯层）", "group": "recycled", "max": 80, "applies": "core", "reason": "三层芯层"},
        {"name": "二级再生料（单层）", "group": "secondary_recycled", "max": 12, "applies": "mono", "reason": "晶点"},
        {"name": "POE 类", "group": "poe", "max": 4, "reason": "挺度与成本"},
        {"name": "EVA 类", "group": "eva", "max": 8, "reason": "粘连与气味（VA ≤ 9%）"},
        {"name": "填充母料", "group": "filler", "max": 0, "reason": "默认不用（改变外观与密度）；确需使用时在此放开上限"},
        {"name": "功能助剂母料", "group": "additive", "min": 0.5, "max": 1.5, "reason": "PPA 有效成分 300～800 ppm"},
    ],
    "component_bounds": {
        "LL": [0, 60], "LLC": [0, 60], "LL6": [0, 30], "mLL": [0, 20], "mLL8": [0, 15], "LD": [0, 20], "HD": [0, 10], "MD": [0, 15],
        "R1": [0, 60], "R2": [0, 40], "RL": [0, 20], "PCR": [0, 20], "SC": [0, 15], "POE": [0, 4], "POP": [0, 5],
        "VL": [0, 10], "EVA": [0, 8], "EMA": [0, 8], "FL": [0, 20], "TFL": [0, 10], "AD": [0.5, 1.5], "CP": [0, 2],
    },
    "pass_rule": {
        "mechanical_ratio": 0.95,
        "mechanical_sd": 1,
        "haze_delta_max": 1.0,
        "transmittance_delta_min": -1.0,
        "note": "拉伸/撕裂/穿刺/热封强度 ≥ base×0.95 且 ≥ base−1SD（无 SD 时只用 0.95）",
    },
}


def load() -> dict[str, Any]:
    c = kb.constraints()
    if not c:
        kb.write_yaml(kb.PROJECT_FILES["constraints"], DEFAULT_CONSTRAINTS)
        c = dict(DEFAULT_CONSTRAINTS)
    # 补齐缺失键 / 新代码
    for k, v in DEFAULT_CONSTRAINTS.items():
        c.setdefault(k, v)
    for g, codes in DEFAULT_CONSTRAINTS["groups"].items():
        c["groups"].setdefault(g, codes)
        for code in codes:
            if code not in c["groups"][g]:
                c["groups"][g].append(code)
    for code, b in DEFAULT_CONSTRAINTS["component_bounds"].items():
        c["component_bounds"].setdefault(code, b)
    # 旧文件迁移：去掉“透明/非透明”分支
    rules = []
    for r in c.get("rules", []):
        if r.get("applies") == "opaque":
            continue
        if r.get("applies") == "transparent":
            r = {k: v for k, v in r.items() if k != "applies"}
        if r.get("name") == "填充母料（透明产品）":
            r["name"] = "填充母料"
        rules.append(r)
    c["rules"] = rules
    return resolve_cost_limits(c)


def resolve_cost_limits(c: dict[str, Any]) -> dict[str, Any]:
    """cost_limit / cost_target 为 auto 时按当前价格卡的 base 成本解析（价格变了自动跟着变）。"""
    from .cost import compute_simple

    base_cost = compute_simple(base_formulation(c))
    if c.get("cost_limit") in (None, "auto"):
        c["cost_limit"] = round(base_cost) if base_cost else 8000
        c["cost_limit_auto"] = True
    if c.get("cost_target") in (None, "auto"):
        c["cost_target"] = round((base_cost or 8000) * 0.95)
        c["cost_target_auto"] = True
    return c


def save(c: dict[str, Any]) -> None:
    c = dict(c)
    # auto 解析出来的数值不落盘，保持“跟随价格卡”
    if c.pop("cost_limit_auto", False):
        c["cost_limit"] = "auto"
    if c.pop("cost_target_auto", False):
        c["cost_target"] = "auto"
    kb.write_yaml(kb.PROJECT_FILES["constraints"], c)


def group_sum(components: dict[str, float], codes: list[str]) -> float:
    return float(sum(components.get(k, 0.0) for k in codes))


_LIB_CACHE: dict[str, Any] = {"at": 0.0, "codes": frozenset()}


def library_codes(ttl: float = 5.0) -> frozenset:
    """原料库里现有的代码。check() 在生成配方时一秒要跑上千次，每次查库太贵，缓存几秒。"""
    import time

    from .materials import list_materials

    if time.monotonic() - _LIB_CACHE["at"] > ttl:
        _LIB_CACHE["codes"] = frozenset(m["code"] for m in list_materials(active_only=True))
        _LIB_CACHE["at"] = time.monotonic()
    return _LIB_CACHE["codes"]


def _any_in_library(codes: list[str]) -> bool:
    """这一组里有没有哪种料是原料库里真有的。"""
    return bool(codes) and bool(set(codes) & library_codes())


def check(components: dict[str, float], *, structure: str = "mono", layer: str = "whole",
          c: dict[str, Any] | None = None) -> list[str]:
    """返回违反约束的说明列表（空 = 可行）。layer: whole | core | skin。"""
    c = c or load()
    comps = {k: float(v) for k, v in components.items() if v}
    issues: list[str] = []
    total = sum(comps.values())
    if abs(total - 100.0) > 0.05:
        issues.append(f"各组分合计 {total:.2f}% ≠ 100%")
    bounds = c.get("component_bounds", {})
    for k, v in comps.items():
        lo, hi = bounds.get(k, [0, 100])
        if v < lo - 1e-9 or v > hi + 1e-9:
            issues.append(f"{k} = {v:g}% 超出单组分范围 [{lo}, {hi}]")
    groups = c.get("groups", {})
    for rule in c.get("rules", []):
        applies = rule.get("applies")
        if applies == "mono" and not (structure == "mono" or layer == "skin"):
            continue
        if applies == "core" and layer != "core":
            continue
        codes = groups.get(rule["group"], [])
        s = group_sum(comps, codes)
        if "min" in rule and not _any_in_library(codes):
            continue          # 这一类料原料库里一种都没有（还没发现/还没询到价），不能拿它判所有配方死刑
        if "min" in rule and s < rule["min"] - 1e-9:
            issues.append(f"{rule['name']} = {s:g}% < 下限 {rule['min']}%（{rule.get('reason', '')}）")
        if "max" in rule and s > rule["max"] + 1e-9:
            issues.append(f"{rule['name']} = {s:g}% > 上限 {rule['max']}%（{rule.get('reason', '')}）")
    return issues


def register_code(code: str, material: dict[str, Any]) -> str:
    """新材料代码自动归入约束分组与单组分范围（按物性），并落盘。返回归入的组名。"""
    c = load()
    groups = c.setdefault("groups", {})
    bounds = c.setdefault("component_bounds", {})
    if any(code in v for v in groups.values()):
        return next(g for g, v in groups.items() if code in v)
    dens = float(material.get("density") or 0)
    cat = str(material.get("category") or "") + str(material.get("name") or "")
    recycled = bool(material.get("is_recycled"))
    if recycled:
        group, bound = "recycled", [0, 60]
    elif any(k in cat for k in ("填充", "CaCO", "碳酸钙", "滑石", "BaSO")):
        group, bound = "filler", [0, 20]
    elif any(k in cat for k in ("EVA", "EMA", "EBA")):
        group, bound = "eva", [0, 8]
    elif any(k in cat for k in ("POE", "POP", "塑性体", "弹性体", "VLDPE", "ULDPE")) or (0 < dens < 0.912):
        group, bound = "poe", [0, 5]
    elif any(k in cat for k in ("HDPE", "高密度")) or dens >= 0.941:
        group, bound = "hdpe", [0, 10]
    elif any(k in cat for k in ("助剂", "母料", "母粒", "PPA", "抗氧")):
        group, bound = "additive", [0, 2]
    else:
        # 密度 0.912～0.94 的新料 PE（LLDPE / mLLDPE / MDPE / LDPE）都按“新料 PE”参与韧性约束
        group, bound = "virgin_pe", [0, 60]
    groups.setdefault(group, []).append(code)
    bounds.setdefault(code, bound)
    save(c)
    return group


def base_formulation(c: dict[str, Any] | None = None) -> dict[str, float]:
    c = c or load()
    return {k: float(v) for k, v in c.get("base_formulation", {}).items()}
