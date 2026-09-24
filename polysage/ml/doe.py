"""首轮实验设计：带约束的混料设计（随机可行点 → Scheffé 二次模型 D-最优交换法），再并入优先配方。"""
from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pandas as pd

from ..formulation import constraints as C
from ..formulation import cost as COST
from ..formulation.generator import generate


def _design_matrix(X: np.ndarray, quadratic: bool = True) -> np.ndarray:
    X = X / 100.0
    cols = [X]
    if quadratic:
        cols.extend(X[:, [i]] * X[:, [j]] for i, j in itertools.combinations(range(X.shape[1]), 2))
    return np.hstack(cols)


def d_optimal(pool: np.ndarray, n_points: int, quadratic: bool = True, iters: int = 30, seed: int = 0) -> list[int]:
    """Fedorov 交换法在候选池中选 n_points 个点使 |X'X| 最大。返回池内索引。"""
    rng = np.random.default_rng(seed)
    F = _design_matrix(pool, quadratic)
    n_pool, p = F.shape
    n_points = min(n_points, n_pool)
    idx = list(rng.choice(n_pool, size=n_points, replace=False))
    ridge = 1e-6 * np.eye(p)

    def logdet(sel: list[int]) -> float:
        M = F[sel].T @ F[sel] + ridge
        s, ld = np.linalg.slogdet(M)
        return ld if s > 0 else -np.inf

    cur = logdet(idx)
    for _ in range(iters):
        improved = False
        for i in range(n_points):
            best_j, best_val = None, cur
            for j in rng.choice(n_pool, size=min(200, n_pool), replace=False):
                if j in idx:
                    continue
                trial = idx.copy()
                trial[i] = int(j)
                v = logdet(trial)
                if v > best_val + 1e-9:
                    best_j, best_val = int(j), v
            if best_j is not None:
                idx[i] = best_j
                cur = best_val
                improved = True
        if not improved:
            break
    return idx


def first_round(n_points: int = 24, variables: list[str] | None = None, priority: list[dict[str, float]] | None = None,
                seed: int = 0, fixed: dict[str, float] | None = None) -> pd.DataFrame:
    """首轮 DOE：只动 variables（默认 LL, LLC, mLL, LD, HD, R1, RL, POE），AD 固定 1%；并入优先配方。"""
    from ..formulation.materials import get_material

    variables = variables or ["LL", "LLC", "mLL", "LD", "HD", "R1", "RL", "POE"]
    if fixed is None:                     # 助剂库里有才固定 1%，原料库是空的时候别卡在这
        fixed = {"AD": 1.0} if get_material("AD") else {}
    # 用生成器造可行池（不限成本），只保留由 variables + fixed 组成的配方
    cands = generate(n_out=3000, n_samples=16000, seed=seed, cost_limit=1e9, fixed=fixed)
    rows = []
    for cd in cands:
        if set(cd.components) - set(variables) - set(fixed):
            continue
        rows.append({v: cd.components.get(v, 0.0) for v in variables} | {"cost": cd.cost, "theme": cd.theme})
    pool = pd.DataFrame(rows)
    if pool.empty:
        raise ValueError("可行池为空，请放宽约束或变量集合")
    sel = d_optimal(pool[variables].values, n_points, seed=seed)
    design = pool.iloc[sel].copy()
    design["来源"] = "D-最优"
    extra = []
    for comps in priority or []:
        r = {v: float(comps.get(v, 0.0)) for v in variables}
        r["cost"] = COST.compute_simple(comps)
        r["theme"] = "优先配方"
        r["来源"] = "Top 20 优先"
        extra.append(r)
    if extra:
        design = pd.concat([design, pd.DataFrame(extra)], ignore_index=True)
    for k, v in fixed.items():
        design[k] = v
    design.insert(0, "sample_id", [f"R1-{i + 1:02d}" for i in range(len(design))])
    design = design.reset_index(drop=True)
    return design


def coverage_report(design: pd.DataFrame, variables: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for v in variables:
        if v in design:
            out[v] = {"min": float(design[v].min()), "max": float(design[v].max()), "n_nonzero": int((design[v] > 0).sum())}
    c = C.load()
    viol = 0
    for _, r in design.iterrows():
        comps = {v: float(r[v]) for v in variables if v in r}
        comps.update({k: float(r[k]) for k in ("AD", "CP") if k in r and r[k]})
        if C.check(comps, c=c):
            viol += 1
    out["constraint_violations"] = viol
    return out
