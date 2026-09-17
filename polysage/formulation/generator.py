"""候选配方生成：按设计思路（谁替 LLDPE 的韧性 → 腾出的空间给谁）随机采样 + 约束过滤 + 成本排序。

定性不定量：这里只产生“满足约束、成本达标、结构多样”的候选；四项性能的定性预期由 qualitative.py 交给 LLM，
精确数值留给实验与 ML。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import constraints as C
from . import cost as COST
from .materials import list_materials

# 设计思路（内部技术路线 4.3）：toughness = 谁来替 LLDPE 的韧性；filler = 腾出来的空间给谁
THEMES: dict[str, dict[str, Any]] = {
    "国产同类 LLDPE 替代": {"toughness": ["LLC"], "space": ["R1"], "optional": ["LD", "HD", "RL"], "ll_free": True},
    "同类替代 + 再生料上调": {"toughness": ["LLC"], "space": ["R1", "RL"], "optional": ["LD", "HD", "POE"], "ll_free": True},
    "茂金属置换": {"toughness": ["mLL"], "space": ["R1", "RL"], "optional": ["LD", "HD", "EVA", "POE"]},
    "C8 茂金属": {"toughness": ["mLL8"], "space": ["R1", "RL"], "optional": ["LD", "HD"]},
    "齐格勒 C6": {"toughness": ["LL6"], "space": ["R1", "RL"], "optional": ["LD", "HD"]},
    "再生线性料": {"toughness": [], "space": ["R1", "RL"], "optional": ["LD", "HD", "POE"]},
    "EVA 保热封": {"toughness": ["mLL"], "space": ["R1"], "optional": ["LD", "HD", "EVA"], "require": ["EVA"]},
    "POE 补韧": {"toughness": [], "space": ["R1"], "optional": ["LD", "HD"], "require": ["POE"]},
    "去 HDPE": {"toughness": [], "space": ["R1"], "optional": ["LD", "mLL"], "forbid": ["HD"]},
    "MDPE 替代": {"toughness": [], "space": ["R1"], "optional": ["MD"], "require": ["MD"], "forbid": ["LD", "HD"]},
    "二级料试探": {"toughness": ["mLL"], "space": ["R1", "R2"], "optional": ["LD", "HD", "POE"], "require": ["R2"]},
    "温和版": {"toughness": ["mLL"], "space": ["R1"], "optional": ["LD", "HD"], "mLL_max": 8},
}

@dataclass
class Candidate:
    components: dict[str, float]
    theme: str
    structure: str = "mono"
    cost: float | None = None
    cost_tier: str = ""
    density: float | None = None
    area_index: float | None = None
    savings: float | None = None
    savings_pct: float | None = None
    issues: list[str] = field(default_factory=list)

    def signature(self, step: float = 3.0) -> tuple:
        return tuple(sorted((k, round(v / step)) for k, v in self.components.items() if v >= 0.5))

    def text(self) -> str:
        return COST.format_components(self.components)


def _round_components(comps: dict[str, float]) -> dict[str, float]:
    """保留 AD 一位小数，其余整数，并把余量调到最大组分使合计 = 100。"""
    small_dose = {"AD", "CP", "NUC", "ADR"}
    # 主料/改性料低于 2% 没有工程意义，直接去掉（助剂类除外）
    out = {k: (round(v, 1) if k in small_dose else float(round(v))) for k, v in comps.items()
           if v > 0.2 and (k in small_dose or v >= 2.0)}
    diff = 100.0 - sum(out.values())
    if out:
        big = max((k for k in out if k != "AD"), key=lambda k: out[k], default=None) or next(iter(out))
        out[big] = round(out[big] + diff, 1)
    return {k: v for k, v in out.items() if v > 0}


def _sample_one(theme: dict[str, Any], rng: random.Random, bounds: dict[str, list[float]],
                mats: dict[str, dict[str, Any]] | None = None) -> dict[str, float]:
    comps: dict[str, float] = {"AD": round(rng.uniform(0.8, 1.2), 1)}
    tough = list(theme.get("toughness", []))
    require = list(theme.get("require", []))
    forbid = set(theme.get("forbid", []))
    optional = [o for o in theme.get("optional", []) if o not in forbid]
    chosen = set(tough + require + theme.get("space", []))
    for o in optional:
        if rng.random() < 0.6:
            chosen.add(o)
    chosen.discard("AD")
    # 主料 LL 始终保留（可以为较低比例）
    chosen.add("LL")
    rest = 100.0 - comps["AD"]
    mats = mats or {}
    for k in chosen:
        lo, hi = bounds.get(k, [0, 30])
        m = mats.get(k) or {}
        # 选中的改性/再生组分按材料卡的典型用量范围取值，避免出现“茂金属 2%”这类无意义比例
        if m.get("typical_min") is not None and k not in ("LL", "R1", "HD"):
            lo = max(lo, float(m["typical_min"]))
        if m.get("typical_max") is not None and k not in ("LL", "R1"):
            hi = min(hi, float(m["typical_max"]))
        if k == "mLL" and theme.get("mLL_max"):
            hi = min(hi, theme["mLL_max"])
        if k == "LL":
            if theme.get("ll_free"):
                # 同类替代主题：现用 LLDPE 要么完全替掉，要么保留 ≥ 8%（做过渡方案）
                if rng.random() < 0.5:
                    continue
                lo, hi = 8, 55
            else:
                lo, hi = 20, 55
        if k in ("R1",):
            lo, hi = max(lo, 20), min(hi, 55)
        if k == "HD":
            hi = min(hi, 5)
        comps[k] = rng.uniform(lo, hi) if hi > lo else lo
    # 归一化非 AD 组分到 rest（保持比例）
    s = sum(v for k, v in comps.items() if k != "AD")
    if s <= 0:
        return {}
    for k in list(comps):
        if k != "AD":
            comps[k] = comps[k] / s * rest
    return COST.ordered(_round_components(comps))


def generate(n_out: int = 40, *, structure: str = "mono", n_samples: int = 20000,
             seed: int = 42, cost_limit: float | None = None, themes: list[str] | None = None,
             fixed: dict[str, float] | None = None, allowed: list[str] | None = None, transparent: bool | None = None) -> list[Candidate]:
    """生成满足约束且成本低于上限的候选，按主题均衡后按成本排序。

    fixed：强制固定的组分；allowed：只允许使用这些材料代码（智能体按“可用材料清单”出方案时用）。
    """
    c = C.load()
    rng = random.Random(seed)
    bounds = c.get("component_bounds", {})
    limit = cost_limit or c.get("cost_limit", 8000)
    theme_pool = dict(THEMES)
    # 原料库里新登记的新料 PE（不在内置主题里的）自动成为“同类替代”主题，价格联动后也能被采样
    known = {x for th in theme_pool.values() for v in th.values() if isinstance(v, list) for x in v} | {"LL", "AD"}
    for code in c.get("groups", {}).get("virgin_pe", []):
        if code not in known and code in {m["code"] for m in list_materials(active_only=True)}:
            theme_pool[f"新料替代（{code}）"] = {"toughness": [code], "space": ["R1", "RL"], "optional": ["LD", "HD"], "ll_free": True}
    if themes:
        theme_pool = {k: v for k, v in theme_pool.items() if k in themes}
    if allowed:
        allow = set(allowed) | {"AD"}
        pruned = {}
        for name, th in theme_pool.items():
            th2 = {k: ([x for x in v if x in allow] if isinstance(v, list) else v) for k, v in th.items()}
            # 主题必需的材料不可用 → 跳过该主题
            if any(x not in allow for x in th.get("toughness", []) + th.get("require", [])):
                continue
            pruned[name] = th2
        theme_pool = pruned or {"可用材料自由组合": {"toughness": [], "space": [x for x in allowed if x in ("R1", "RL", "R2")], "optional": [x for x in allowed if x not in ("AD",)]}}
    mats = {m["code"]: m for m in list_materials(active_only=False)}
    base = C.base_formulation(c)
    seen: set[tuple] = set()
    per_theme: dict[str, list[Candidate]] = {k: [] for k in theme_pool}
    names = list(theme_pool)
    for i in range(n_samples):
        tname = names[i % len(names)]
        comps = _sample_one(theme_pool[tname], rng, bounds, mats)
        if not comps:
            continue
        if allowed and any(k not in allowed and k != "AD" for k in comps):
            continue
        if fixed:
            comps = _apply_fixed(comps, fixed)
        issues = C.check(comps, structure=structure, c=c)
        if issues:
            continue
        cand = Candidate(components=comps, theme=tname, structure=structure)
        sig = cand.signature()
        if sig in seen:
            continue
        res = COST.compute(comps, base, mats)
        if res.cost_used is None or res.cost_used >= limit:
            continue
        seen.add(sig)
        cand.cost = round(res.cost_used)
        cand.cost_tier = res.tier
        cand.density = round(res.density, 3) if res.density else None
        cand.area_index = round(res.area_index, 1) if res.area_index else None
        cand.savings, cand.savings_pct = COST.savings_vs_base(res.cost_used, base)
        per_theme[tname].append(cand)
    # 每个主题取最便宜的若干，再汇总排序
    quota = max(3, n_out // max(1, len(names)) + 1)
    pool: list[Candidate] = []
    for tname, lst in per_theme.items():
        lst.sort(key=lambda x: x.cost or 1e9)
        pool.extend(lst[:quota])
    pool.sort(key=lambda x: x.cost or 1e9)
    return pool[:n_out]


def ladder_candidates(*, structure: str = "mono", ad: float = 1.0,
                      recycled_steps: tuple[float, ...] = (15, 25, 35, 45)) -> list[Candidate]:
    """确定性的“阶梯”候选（方案 V3 §1.1/1.2）：

    - 同类替代（1:1）：现用 LLDPE 全部/一半换成 LLC / LL6，其余组分不动；
    - 再生料阶梯：在 1:1 替代基础上，再生料按 15/25/35/45% 阶梯替代 LLDPE；
    - 茂金属低比例替代：mLL 10～15% 顶替 25～30 个点 LLDPE，空出的给再生料。
    这些是“先保性能再看成本”的保守方案，和随机生成的低成本方案一起送评估。
    """
    c = C.load()
    base = C.base_formulation(c)
    mats = {m["code"]: m for m in list_materials(active_only=True)}
    out: list[Candidate] = []

    def _mk(theme: str, comps: dict[str, float]) -> None:
        comps = {k: float(v) for k, v in comps.items() if v and v > 0}
        s = sum(comps.values())
        if abs(s - 100) > 0.05:
            big = max(comps, key=comps.get)
            comps[big] = round(comps[big] + (100 - s), 1)
        if C.check(comps, structure=structure, c=c):
            return
        cd = evaluate(comps, structure=structure)
        cd.theme = theme
        if cd.cost is not None:
            out.append(cd)

    ll = base.get("LL", 0.0)
    others = {k: v for k, v in base.items() if k != "LL"}
    # 助剂占位：从最大组分里扣
    def _with_ad(d: dict[str, float]) -> dict[str, float]:
        d = dict(d)
        if ad and "AD" in mats:
            # 助剂从新料主体里扣（保持再生料阶梯的名义比例不变）
            host = next((k for k in ("LL", "LLC", "LL6") if d.get(k, 0) >= ad + 5), max(d, key=d.get))
            d[host] = round(d[host] - ad, 1)
            d["AD"] = ad
        return d

    for sub in ("LLC", "LL6"):
        if sub not in mats:
            continue
        _mk(f"同类替代 1:1（{sub}）", _with_ad({sub: ll, **others}))
        _mk(f"同类替代 50%（{sub}）", _with_ad({"LL": ll / 2, sub: ll / 2, **others}))
    r1_base = base.get("R1", 0.0)
    for step in recycled_steps:
        delta = step - r1_base
        if delta <= 0 or ll - delta < 10:
            continue
        d = {**others, "R1": step, "LL": ll - delta}
        _mk(f"再生料阶梯 {step:g}%（现用 LLDPE）", _with_ad(d))
        if "LLC" in mats:
            _mk(f"再生料阶梯 {step:g}%（LLC 替代）", _with_ad({**others, "R1": step, "LLC": ll - delta}))
    for m_pct, ll_cut in ((10, 25), (15, 30)):
        if "mLL" in mats and ll - ll_cut >= 10:
            _mk(f"茂金属 {m_pct}% 低比例替代", _with_ad({**others, "mLL": m_pct, "LL": ll - ll_cut, "R1": r1_base + (ll_cut - m_pct)}))
    return out


def _apply_fixed(comps: dict[str, float], fixed: dict[str, float]) -> dict[str, float]:
    out = {k: v for k, v in comps.items() if k not in fixed}
    rest = 100.0 - sum(fixed.values())
    s = sum(out.values())
    if s <= 0 or rest <= 0:
        return {}
    out = {k: v / s * rest for k, v in out.items()}
    out.update(fixed)
    return _round_components(out)


def evaluate(components: dict[str, float], *, structure: str = "mono") -> Candidate:
    """对单个配方做约束检查与成本核算（供工具调用 / 手工输入）。"""
    comps = {k: float(v) for k, v in components.items() if v}
    c = C.load()
    issues = C.check(comps, structure=structure, c=c)
    res = COST.compute(comps, C.base_formulation(c))
    cand = Candidate(components=COST.ordered(comps), theme="手工", structure=structure, issues=issues)
    if res.cost_used is not None:
        cand.cost = round(res.cost_used)
        cand.cost_tier = res.tier
        cand.savings, cand.savings_pct = COST.savings_vs_base(res.cost_used)
    cand.density = round(res.density, 3) if res.density else None
    cand.area_index = round(res.area_index, 1) if res.area_index else None
    return cand


def feasible_grid(n: int = 2000, seed: int = 0) -> np.ndarray:
    """给 ML/BO 用的候选空间：返回 DataFrame 形式的可行配方（含成本）。"""
    import pandas as pd

    cands = generate(n_out=n, n_samples=n * 6, seed=seed, cost_limit=1e9)
    codes = sorted({k for cd in cands for k in cd.components})
    rows = []
    for cd in cands:
        r = {k: cd.components.get(k, 0.0) for k in codes}
        r["cost"] = cd.cost
        r["theme"] = cd.theme
        rows.append(r)
    return pd.DataFrame(rows)
