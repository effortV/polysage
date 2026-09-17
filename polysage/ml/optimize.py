"""AI + ML 联合推荐（内部技术路线 6.x）：

目标：最小化成本；约束：P(四项预测 ≥ 阈值) ≥ 0.8（用 GP 均值与 SD）；满足配方约束；处于训练域附近。
输出：利用型（预测最好）+ 探索型（不确定度大但可能更便宜）。另提供 NSGA-II 的成本—性能裕度 Pareto 前沿。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from ..formulation import constraints as C
from ..formulation.generator import feasible_grid
from ..formulation.library import list_formulations
from . import dataset as D
from .models import predict


def _p_pass(pred: pd.DataFrame, thr: dict[str, float], targets: list[str]) -> np.ndarray:
    p = np.ones(len(pred))
    for t in targets:
        if t + "_mean" not in pred or t not in thr:
            continue
        mu = pred[t + "_mean"].values
        sd = pred[t + "_sd"].fillna(np.nanmean(pred[t + "_sd"]) if pred[t + "_sd"].notna().any() else 0.0).values
        sd = np.where(sd <= 1e-9, 1e-9, sd)
        z = (mu - thr[t]) / sd
        if not D.HIGHER_BETTER.get(t, True):
            z = -z
        p *= norm.cdf(z)
    return p


def _train_domain_distance(cands: pd.DataFrame, train: pd.DataFrame, cols: list[str]) -> np.ndarray:
    A = cands[cols].values / 100.0
    B = train[cols].values / 100.0
    d = np.sqrt(((A[:, None, :] - B[None, :, :]) ** 2).sum(-1))
    return d.min(axis=1)


def recommend(n_exploit: int = 4, n_explore: int = 3, p_min: float = 0.8, max_distance: float = 0.15,
              seed: int = 0, mats: dict[str, dict[str, Any]] | None = None,
              targets: list[str] | None = None) -> dict[str, Any]:
    df = D.load()
    agg = D.aggregate(df)
    base = D.base_stats(agg)
    if not base:
        raise ValueError("缺少 base（sample_id 以 S0 开头的现用膜数据）")
    thr = D.thresholds(base)
    targets = [t for t in (targets or D.PRIMARY_TARGETS) if t in thr]
    # 候选空间 = 配方库未试验 + 约束内随机 2000
    grid = feasible_grid(2000, seed=seed)
    lib_rows = []
    for f in list_formulations():
        if f["status"] in ("候选", "推荐"):
            r = {c: float(f["components"].get(c, 0.0)) for c in D.COMP_COLS}
            r["cost"] = f.get("cost_used")
            r["theme"] = f["code"]
            lib_rows.append(r)
    cands = pd.concat([pd.DataFrame(lib_rows), grid], ignore_index=True) if lib_rows else grid
    for c in D.COMP_COLS:
        if c not in cands:
            cands[c] = 0.0
    cands[D.COMP_COLS] = cands[D.COMP_COLS].fillna(0.0)
    # 排除已试验过的配方
    tested = agg[D.COMP_COLS].round(0).astype(int).astype(str).agg("-".join, axis=1).tolist()
    key = cands[D.COMP_COLS].round(0).astype(int).astype(str).agg("-".join, axis=1)
    cands = cands[~key.isin(tested)].reset_index(drop=True)
    comps_list = [{c: float(r[c]) for c in D.COMP_COLS if r[c]} for _, r in cands.iterrows()]
    pred = predict(comps_list, targets=targets, mats=mats)
    if pred.empty:
        raise ValueError("尚无已训练模型，请先在“实验与模型”页训练")
    cands = pd.concat([cands, pred], axis=1)
    cands["p_pass"] = _p_pass(pred, thr, targets)
    cands["dist"] = _train_domain_distance(cands, agg, D.COMP_COLS)
    sd_cols = [t + "_sd" for t in targets if t + "_sd" in cands]
    cands["uncertainty"] = cands[sd_cols].fillna(0).mean(axis=1) if sd_cols else 0.0
    # 裕度：各目标 (mu - thr)/thr 的最小值
    margins = []
    for t in targets:
        m = (cands[t + "_mean"] - thr[t]) / (abs(thr[t]) + 1e-9)
        margins.append(m if D.HIGHER_BETTER.get(t, True) else -m)
    cands["min_margin"] = pd.concat(margins, axis=1).min(axis=1) if margins else 0.0
    c = C.load()
    limit = c.get("cost_limit", 8000)
    in_domain = cands[(cands["dist"] <= max_distance) & (cands["cost"] < limit)]
    exploit = in_domain[in_domain["p_pass"] >= p_min].sort_values(["cost", "min_margin"], ascending=[True, False]).head(n_exploit)
    note = ""
    if exploit.empty:
        # 没有达到 P(过关) ≥ p_min 的候选：给出最接近过关的（按 P 与裕度），并明确标注
        exploit = in_domain.sort_values(["p_pass", "min_margin"], ascending=[False, False]).head(n_exploit)
        exploit = exploit.assign(type=f"最接近过关（未达 P≥{p_min:.0%}）")
        note = "当前模型下没有候选达到 P(过关) ≥ %.0f%%；建议下一轮向 base 方向收缩或补充数据。" % (p_min * 100)
    else:
        exploit = exploit.assign(type="利用型")
    best_cost = exploit["cost"].min() if not exploit.empty else limit
    p_floor = min(0.4, float(cands["p_pass"].max()) * 0.5) if len(cands) else 0.4
    explore_pool = cands[(cands["cost"] < best_cost) & (cands["p_pass"] >= p_floor) & (~cands.index.isin(exploit.index))]
    explore = explore_pool.sort_values("uncertainty", ascending=False).head(n_explore).assign(type="探索型")
    rec = pd.concat([exploit, explore]).reset_index(drop=True)
    return {"recommendations": rec, "thresholds": thr, "base": base, "targets": targets, "n_candidates": len(cands),
            "candidates": cands, "note": note}


def pareto_front(cands: pd.DataFrame, n_gen: int = 40, pop: int = 60, seed: int = 0) -> pd.DataFrame:
    """用 NSGA-II 在候选集上做 min 成本 / max 最小裕度 的 Pareto 前沿（若 pymoo 不可用则退化为非支配筛选）。"""
    if cands.empty:
        return cands
    try:
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.core.problem import Problem
        from pymoo.optimize import minimize

        costs = cands["cost"].values.astype(float)
        margins = cands["min_margin"].values.astype(float)
        n = len(cands)

        class Pick(Problem):
            def __init__(self):
                super().__init__(n_var=1, n_obj=2, xl=0, xu=n - 1, vtype=int)

            def _evaluate(self, x, out, *args, **kwargs):
                i = np.clip(np.round(x[:, 0]).astype(int), 0, n - 1)
                out["F"] = np.column_stack([costs[i], -margins[i]])

        res = minimize(Pick(), NSGA2(pop_size=min(pop, n)), ("n_gen", n_gen), seed=seed, verbose=False)
        idx = np.unique(np.clip(np.round(res.X[:, 0]).astype(int), 0, n - 1))
        return cands.iloc[idx].sort_values("cost").reset_index(drop=True)
    except Exception:  # noqa: BLE001
        pass
    # 非支配筛选
    arr = cands[["cost", "min_margin"]].values
    keep = []
    for i, (c1, m1) in enumerate(arr):
        dominated = np.any((arr[:, 0] <= c1) & (arr[:, 1] >= m1) & ((arr[:, 0] < c1) | (arr[:, 1] > m1)))
        if not dominated:
            keep.append(i)
    return cands.iloc[keep].sort_values("cost").reset_index(drop=True)
