"""配方 → 性能 模型：Scheffé 混合模型 / 梯度提升 / 高斯过程，每项性能单独建模，LOOCV 选型。

验收（内部技术路线 5.6）：LOOCV RMSE ≤ base 的 8%（力学/热封）；雾度 RMSE ≤ 1.0；过关分类准确率 ≥ 80%。
"""
from __future__ import annotations

import itertools
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin, clone
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process import kernels as K
from sklearn.linear_model import Ridge
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .. import db
from ..config import KB
from . import dataset as D

warnings.filterwarnings("ignore", category=ConvergenceWarning)


class ScheffeQuadratic(BaseEstimator, RegressorMixin):
    """Scheffé 二次混合模型：y = Σ b_i x_i + Σ b_ij x_i x_j（无截距，岭回归防病态）。"""

    def __init__(self, alpha: float = 1.0, quadratic: bool = True):
        self.alpha = alpha
        self.quadratic = quadratic

    def _expand(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float) / 100.0
        cols = [X]
        if self.quadratic:
            n = X.shape[1]
            cols.extend(X[:, [i]] * X[:, [j]] for i, j in itertools.combinations(range(n), 2))
        return np.hstack(cols)

    def fit(self, X, y):
        self.model_ = Ridge(alpha=self.alpha, fit_intercept=False).fit(self._expand(X), y)
        return self

    def predict(self, X):
        return self.model_.predict(self._expand(X))


def candidate_models(n: int) -> dict[str, Any]:
    models: dict[str, Any] = {
        "scheffe_linear": ScheffeQuadratic(alpha=1e-4, quadratic=False),
    }
    if n >= 15:
        models["scheffe_quadratic"] = ScheffeQuadratic(alpha=1e-3, quadratic=True)
    if n >= 20:
        models["gbr"] = GradientBoostingRegressor(n_estimators=300, max_depth=2, learning_rate=0.05, subsample=0.9,
                                                  random_state=0)
    if n >= 12:
        kernel = K.ConstantKernel(1.0, (1e-2, 1e2)) * K.Matern(length_scale=np.ones(1), nu=2.5) + K.WhiteKernel(1e-2, (1e-5, 1e1))
        models["gpr"] = make_pipeline(StandardScaler(), GaussianProcessRegressor(kernel=kernel, normalize_y=True,
                                                                                 n_restarts_optimizer=3, random_state=0))
    return models


@dataclass
class TrainReport:
    target: str
    n: int
    best: str
    metrics: dict[str, dict[str, float]]
    residuals: pd.DataFrame
    file_path: str = ""
    accepted: bool | None = None
    note: str = ""
    features: list[str] = field(default_factory=list)


def _fit_gpr_kernel_dim(model: Any, d: int) -> Any:
    """GP 的 Matern length_scale 需与特征维度一致（ARD）。"""
    if hasattr(model, "steps"):
        gp = model.steps[-1][1]
        gp.kernel = K.ConstantKernel(1.0, (1e-2, 1e2)) * K.Matern(length_scale=np.ones(d), nu=2.5) + K.WhiteKernel(1e-2, (1e-5, 1e1))
    return model


def train_target(X: pd.DataFrame, y: pd.Series, target: str, base_mean: float | None = None,
                 save_dir: Path | None = None) -> TrainReport:
    mask = y.notna()
    Xv, yv = X[mask].values, y[mask].values
    n = len(yv)
    if n < 6:
        raise ValueError(f"{target}: 有效样本 {n} < 6，无法建模")
    metrics: dict[str, dict[str, float]] = {}
    preds: dict[str, np.ndarray] = {}
    for name, m in candidate_models(n).items():
        m = _fit_gpr_kernel_dim(clone(m), Xv.shape[1]) if name == "gpr" else clone(m)
        try:
            p = cross_val_predict(m, Xv, yv, cv=LeaveOneOut())
        except Exception:  # noqa: BLE001
            continue
        rmse = float(np.sqrt(np.mean((p - yv) ** 2)))
        ss_res = float(np.sum((p - yv) ** 2))
        ss_tot = float(np.sum((yv - yv.mean()) ** 2)) or 1e-9
        metrics[name] = {"rmse": rmse, "mae": float(np.mean(np.abs(p - yv))), "r2_loocv": 1 - ss_res / ss_tot}
        preds[name] = p
    if not metrics:
        raise ValueError(f"{target}: 所有模型训练失败")
    best = min(metrics, key=lambda k: metrics[k]["rmse"])
    final = candidate_models(n)[best]
    final = _fit_gpr_kernel_dim(clone(final), Xv.shape[1]) if best == "gpr" else clone(final)
    final.fit(Xv, yv)
    # 另训一个 GP 供不确定度估计（若样本允许）
    gp_model = None
    if "gpr" in candidate_models(n):
        gp_model = _fit_gpr_kernel_dim(clone(candidate_models(n)["gpr"]), Xv.shape[1]).fit(Xv, yv)
    resid = pd.DataFrame({"sample_id": X.index[mask] if X.index.name == "sample_id" else np.arange(n),
                          "actual": yv, "pred_loocv": preds[best], "residual": preds[best] - yv})
    accepted = None
    note = ""
    if base_mean:
        lim = 0.08 * base_mean
        accepted = metrics[best]["rmse"] <= lim
        note = f"RMSE {metrics[best]['rmse']:.3g}（验收 ≤ base 8% = {lim:.3g}）"
    save_dir = save_dir or KB["model"]
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"{target}.joblib"
    joblib.dump({"model": final, "gp": gp_model, "features": list(X.columns), "best": best, "metrics": metrics,
                 "trained_at": db.now(), "n": n}, path)
    db.execute("UPDATE models SET is_current=0 WHERE target=?", (target,))
    db.insert("models", {"target": target, "model_type": best, "n_samples": n, "metrics_json": db.dumps(metrics),
                         "features_json": db.dumps(list(X.columns)), "file_path": str(path), "trained_at": db.now(),
                         "is_current": 1})
    return TrainReport(target, n, best, metrics, resid, str(path), accepted, note, list(X.columns))


def train_all(df: pd.DataFrame | None = None, targets: list[str] | None = None, use_process: bool = False,
              mats: dict[str, dict[str, Any]] | None = None) -> tuple[list[TrainReport], dict[str, Any]]:
    df = D.load() if df is None else df
    agg = D.aggregate(df).set_index("sample_id")
    base = D.base_stats(agg.reset_index())
    X = D.feature_frame(agg, use_process=use_process, mats=mats)
    reports = []
    for t in (targets or D.PRIMARY_TARGETS):
        if t not in agg or agg[t].notna().sum() < 6:
            continue
        try:
            reports.append(train_target(X, agg[t], t, base_mean=(base.get(t) or {}).get("mean")))
        except ValueError as e:
            reports.append(TrainReport(t, int(agg[t].notna().sum()), "", {}, pd.DataFrame(), note=str(e)))
    # 过关分类准确率（用各目标的 LOOCV 预测）
    summary: dict[str, Any] = {"base": base, "thresholds": D.thresholds(base) if base else {}}
    if base and reports:
        thr = summary["thresholds"]
        pred_ok = pd.Series(True, index=agg.index)
        true_ok = pd.Series(True, index=agg.index)
        used = 0
        for r in reports:
            if r.residuals.empty or r.target not in thr:
                continue
            p = pd.Series(r.residuals["pred_loocv"].values, index=agg.index[agg[r.target].notna()])
            a = agg[r.target]
            hb = D.HIGHER_BETTER.get(r.target, True)
            pred_ok &= (p >= thr[r.target]) if hb else (p <= thr[r.target])
            true_ok &= (a >= thr[r.target]) if hb else (a <= thr[r.target])
            used += 1
        if used:
            summary["pass_accuracy"] = float((pred_ok == true_ok).mean())
    return reports, summary


def load_model(target: str) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM models WHERE target=? AND is_current=1 ORDER BY id DESC", (target,))
    if not r or not Path(r["file_path"]).exists():
        return None
    return joblib.load(r["file_path"])


def predict(components_list: list[dict[str, float]], targets: list[str] | None = None,
            mats: dict[str, dict[str, Any]] | None = None) -> pd.DataFrame:
    """对若干配方预测各目标：mean / sd（sd 来自 GP，无 GP 时为 NaN）。"""
    targets = targets or D.PRIMARY_TARGETS
    rows = []
    for comps in components_list:
        rows.append({c: float(comps.get(c, 0.0)) for c in D.COMP_COLS})
    agg = pd.DataFrame(rows)
    out = pd.DataFrame(index=agg.index)
    for t in targets:
        bundle = load_model(t)
        if not bundle:
            continue
        X = D.feature_frame(agg, use_process=False, mats=mats)
        X = X.reindex(columns=bundle["features"], fill_value=0.0)
        out[t + "_mean"] = bundle["model"].predict(X.values)
        gp = bundle.get("gp")
        if gp is not None:
            try:
                _, sd = gp.predict(X.values, return_std=True)
                out[t + "_sd"] = sd
            except Exception:  # noqa: BLE001
                out[t + "_sd"] = np.nan
        else:
            out[t + "_sd"] = np.nan
    return out


def current_models() -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM models WHERE is_current=1 ORDER BY target")
    for r in rows:
        r["metrics"] = db.loads(r.get("metrics_json"), {})
    return rows
