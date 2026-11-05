"""实验闭环：首轮试验设计 → 回填实测 → 用数据校准系数 → 推荐下一轮。

现在没有数据，性能全靠定性系数（rules.py）。每做一轮实验回填进来，就用岭回归把系数往数据拉
（数据少时系数基本不动，数据多了才明显改）；同时从现配方对照组拿到拉断力的绝对值（N）。
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .. import db, kb
from ..config import KB
from . import rules as R
from .model import DEFAULT_T_REF, Ctx, Design, base_design, layer_text

GRADE_CENTER = {0: 0.3, 1: 1.05, 2: 1.8}     # 透光档 → 透光分的中位，用来校准透光系数
RIDGE = 2.0                                  # 系数往先验收缩的力度：数据越少越不动
TEST_NOTE = [
    "拉断力：按 GB/T 1040.3 薄膜拉伸，样条宽 15 mm，纵向（MD）、横向（TD）各测不少于 3 条，填平均值（N）。",
    "透光：对照三块标样目视判档——0 一般 / 1 好 / 2 很好；拿不准就填偏低的那档。",
    "实测厚度：测厚仪取 5 点平均（丝，1 丝 = 10 μm）。设定厚度和实测差别大时以实测为准。",
    "同一组的工艺（温度、吹胀比、牵引速度）尽量和现配方保持一致，只改配方和厚度。",
]


# ---------------- 试验记录 ----------------

def trials(round_no: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM aba_trials" + (" WHERE round=?" if round_no is not None else "") + " ORDER BY round, id"
    rows = db.q(sql, (round_no,) if round_no is not None else ())
    for r in rows:
        r["design"] = Design.from_dict(db.loads(r["design_json"], {}) or {})
        r["predicted"] = db.loads(r.get("predicted_json"), {}) or {}
    return rows


def measured() -> list[dict[str, Any]]:
    return [r for r in trials() if r.get("md_n") or r.get("td_n") or r.get("grade") is not None]


def next_round_no() -> int:
    r = db.q1("SELECT MAX(round) m FROM aba_trials")
    return int((r or {}).get("m") or 0) + 1


def _save_round(items: list[tuple[Design, str]], t_ref: float) -> tuple[int, list[dict[str, Any]]]:
    rn = next_round_no()
    ctx = Ctx()
    ref = base_design(t_ref)
    out = []
    for i, (d, purpose) in enumerate(items, 1):
        code = f"T{rn}-{i:02d}"
        pred = ctx.evaluate(d, ref)
        pred = {k: pred[k] for k in ("cost", "gsm", "md_rel", "td_rel", "grade", "grade_label", "confidence") if k in pred}
        db.insert("aba_trials", {"round": rn, "code": code, "purpose": purpose, "design_json": db.dumps(d.to_dict()),
                                 "predicted_json": db.dumps(pred), "created_at": db.now()})
        out.append({"code": code, "purpose": purpose, "design": d, "predicted": pred})
    return rn, out


# ---------------- 首轮：D-最优 ----------------

def first_round(t_ref: float = DEFAULT_T_REF, n_points: int = 11, seed: int = 0,
                extra: list[Design] | None = None) -> dict[str, Any]:
    """现配方在三个厚度下各一组（拿到拉断力绝对值和厚度效应）+ D-最优挑出的 n_points 组（覆盖再生比例、
    B 层填充、B 层 HDPE、A 层占比、厚度五个因素）+ 你指定要加的组。"""
    from ..ml.doe import d_optimal

    refs = [(base_design(t), "现配方对照") for t in (3.5, 4.5, 5.5)]
    pool, designs = [], []
    for r in (20.0, 33.3, 45.0):
        for f in (0.0, 15.0, 30.0, 45.0):
            for h in (0.0, 10.0):
                for a in (0.35, 0.45):
                    for t in (3.5, 4.5, 5.5):
                        base = {"LL": 100 - r, "R1": r}
                        b = {k: v * (100 - f - h) / 100 for k, v in base.items()}
                        if f:
                            b["FL"] = f
                        if h:
                            b["HD"] = h
                        d = Design(a=dict(base), b={k: round(v, 2) for k, v in b.items()}, a_ratio=a, thickness=t)
                        pool.append([r / 45, f / 45, h / 10, (a - 0.35) / 0.1, (t - 3.5) / 2])
                        designs.append(d)
    idx = d_optimal(np.array(pool), n_points, quadratic=False, seed=seed)
    picked = [(designs[i], "D-最优") for i in idx]
    picked += [(d, "指定") for d in (extra or [])]
    rn, saved = _save_round(refs + picked, t_ref)
    path = export_round(rn, t_ref)
    return {"round": rn, "trials": saved, "path": str(path)}


# ---------------- 导出 / 回填 ----------------

def _kg(d: Design, batch_kg: float) -> dict[str, float]:
    out = {}
    for layer, comp, frac in (("A层", d.a, d.a_ratio), ("B层", d.b, 1 - d.a_ratio)):
        for k, v in comp.items():
            out[f"{layer} {k} kg"] = round(batch_kg * frac * v / 100, 2)
    return out


def export_round(round_no: int, t_ref: float = DEFAULT_T_REF, batch_kg: float = 100.0) -> Path:
    rows = trials(round_no)
    plan, record = [], []
    for r in rows:
        d: Design = r["design"]
        p = r["predicted"]
        plan.append({"编号": r["code"], "目的": r["purpose"], "A层占比": f"{d.a_ratio:.0%}", "B层占比": f"{1 - d.a_ratio:.0%}",
                     "设定厚度(丝)": d.thickness, "A层配方(%)": layer_text(d.a), "B层配方(%)": layer_text(d.b),
                     "预计成本(元/吨)": p.get("cost"), "预测纵向(×现配方)": p.get("md_rel"), "预测横向(×现配方)": p.get("td_rel"),
                     "预测透光": p.get("grade_label"), **_kg(d, batch_kg)})
        record.append({"编号": r["code"], "设定厚度(丝)": d.thickness, "实测厚度(丝)": None, "纵向拉断力(N)": None,
                       "横向拉断力(N)": None, "透光档(0一般/1好/2很好)": None, "备注": ""})
    path = KB["experiment"] / f"ABA第{round_no}轮试验方案_{date.today().isoformat()}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(plan).to_excel(xw, sheet_name="试验配方", index=False)
        pd.DataFrame(record).to_excel(xw, sheet_name="数据记录", index=False)
        pd.DataFrame({"说明": TEST_NOTE + [f"投料按每组 {batch_kg:g} kg 成品折算；A 层两面同配方，kg 是两面合计。"]}).to_excel(
            xw, sheet_name="说明", index=False)
    return path


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def import_results(source: Any) -> dict[str, Any]:
    """读回填好的“数据记录”页（xlsx 路径 / 上传文件 / DataFrame），按编号写回。"""
    df = source if isinstance(source, pd.DataFrame) else pd.read_excel(source, sheet_name="数据记录")
    cols = {c: str(c) for c in df.columns}
    find = lambda key: next((c for c, s in cols.items() if key in s), None)  # noqa: E731
    c_code, c_t, c_md, c_td, c_g, c_note = (find("编号"), find("实测厚度"), find("纵向"), find("横向"), find("透光"), find("备注"))
    if not c_code:
        raise ValueError("找不到“编号”列：请用导出的试验方案表回填")
    n, skipped, problems = 0, [], []
    for _, row in df.iterrows():
        code = str(row[c_code]).strip()
        if not code or code == "nan":
            continue
        t, md, td = _num(row.get(c_t)), _num(row.get(c_md)), _num(row.get(c_td))
        g = _num(row.get(c_g)) if c_g else None
        if md is None and td is None and g is None:
            skipped.append(code)
            continue
        if g is not None and int(g) not in (0, 1, 2):
            problems.append(f"{code} 透光档 {g:g} 不是 0/1/2")
            g = None
        exist = db.q1("SELECT id FROM aba_trials WHERE code=?", (code,))
        if not exist:
            problems.append(f"{code} 不在试验记录里")
            continue
        db.update("aba_trials", exist["id"], {"t_measured": t, "md_n": md, "td_n": td,
                                              "grade": None if g is None else int(g),
                                              "note": str(row.get(c_note) or "")[:200] if c_note else "",
                                              "measured_at": db.now()})
        n += 1
    return {"updated": n, "skipped": skipped, "problems": problems}


# ---------------- 校准 ----------------

def _features(ctx: Ctx, d: Design, roles: list[str]) -> np.ndarray:
    agg = {r: 0.0 for r in roles}
    for k, share in d.whole().items():
        role = ctx.role.get(k, "additive")
        if role in agg:
            agg[role] += share / 10
    return np.array([agg[r] for r in roles])


def _ridge_to_prior(X: np.ndarray, y: np.ndarray, prior: np.ndarray, lam: float) -> tuple[float, np.ndarray]:
    """y ≈ α + X β，β 往 prior 收缩：最小化 Σ(y − α − Xβ)² + λ‖β − prior‖²（α 不收缩）。"""
    y2 = y - X @ prior
    Xc = X - X.mean(axis=0)
    yc = y2 - y2.mean()
    delta = np.linalg.solve(Xc.T @ Xc + lam * np.eye(X.shape[1]), Xc.T @ yc)
    beta = prior + delta
    alpha = float(np.mean(y - X @ beta))
    return alpha, beta


def _ordinal_to_prior(X: np.ndarray, g: np.ndarray, prior: np.ndarray, s0: float, lam: float,
                      tau: float = 0.15) -> tuple[float, np.ndarray]:
    """透光是 0/1/2 三档：分界固定在 rules.GRADE_CUTS，拟合透光分 s = α + Xβ 使档位的似然最大，
    同时 β 往先验、α 往 S0 收缩。分界固定，系数的尺度才和先验可比（拿档位中位数直接回归会把系数压扁）。"""
    from scipy.optimize import minimize

    c1, c2 = R.GRADE_CUTS
    sig = lambda z: 1 / (1 + np.exp(-np.clip(z, -30, 30)))  # noqa: E731
    prior = prior.astype(float)

    def objective(theta: np.ndarray) -> tuple[float, np.ndarray]:
        alpha, beta = theta[0], theta[1:]
        s = alpha + X @ beta
        p1, p2 = sig((s - c1) / tau), sig((s - c2) / tau)          # P(档 ≥ 1)、P(档 ≥ 2)
        probs = np.clip(np.where(g == 0, 1 - p1, np.where(g == 1, p1 - p2, p2)), 1e-12, 1)
        d1, d2 = p1 * (1 - p1) / tau, p2 * (1 - p2) / tau
        ds = np.where(g == 0, -d1, np.where(g == 1, d1 - d2, d2)) / probs   # ∂ log p / ∂ s
        loss = -float(np.sum(np.log(probs))) + lam * float(np.sum((beta - prior) ** 2)) + lam * (alpha - s0) ** 2
        grad = np.concatenate([[-float(np.sum(ds)) + 2 * lam * (alpha - s0)], -(X.T @ ds) + 2 * lam * (beta - prior)])
        return loss, grad

    res = minimize(objective, np.concatenate([[s0], prior]), jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return float(res.x[0]), res.x[1:]


def calibrate(t_ref: float = DEFAULT_T_REF, lam: float = RIDGE) -> dict[str, Any]:
    rows = measured()
    if len(rows) < 4:
        return {"ok": False, "n": len(rows), "reason": f"已回填 {len(rows)} 组，至少要 4 组（含现配方对照）才校准。"}
    ctx = Ctx(table=R.load_table(include_cal=False))      # 先验 = 默认 + 用户改过的，不叠上一次的校准
    used = sorted({ctx.role.get(k, "additive") for r in rows for k in r["design"].whole()} - {"lldpe"})
    ref = base_design(t_ref)
    out: dict[str, Any] = {"n": len(rows), "date": db.now(), "roles": {}}
    x_ref = _features(ctx, ref, used)
    for key, col in (("md", "md_n"), ("td", "td_n")):
        data = [(r["design"], r[col], r.get("t_measured") or r["design"].thickness) for r in rows if r.get(col)]
        if len(data) < 4:
            continue
        X = np.array([_features(ctx, d, used) for d, _, _ in data])
        y = np.array([math.log(v / t) for _, v, t in data])           # 拉断力 / 厚度 ≈ 强度（对数线性）
        prior = np.array([ctx.table[r][key] for r in used])
        alpha, beta = _ridge_to_prior(X, y, prior, lam)
        resid = y - (alpha + X @ beta)
        out[f"{key}_N_ref"] = round(math.exp(alpha + float(x_ref @ beta)) * t_ref, 2)
        out[f"{key}_rmse_pct"] = round(float(np.sqrt(np.mean(resid ** 2))) * 100, 1)
        for r_, b in zip(used, beta):
            out["roles"].setdefault(r_, {})[key] = round(float(b), 4)
    gdata = [(r["design"], r["grade"], r.get("t_measured") or r["design"].thickness) for r in rows if r.get("grade") is not None]
    if len(gdata) >= 4:
        X = np.array([np.append(_features(ctx, d, used), t - t_ref) for d, _, t in gdata])
        g = np.array([int(gr) for _, gr, _ in gdata])
        prior = np.array([ctx.table[r]["trans"] for r in used] + [R.THICK_TRANS])
        alpha, beta = _ordinal_to_prior(X, g, prior, R.S0, lam)
        for r_, b in zip(used, beta[:-1]):
            out["roles"].setdefault(r_, {})["trans"] = round(float(b), 4)
        out["thick_trans"] = round(float(beta[-1]), 4)
        out["trans_intercept"] = round(alpha, 3)
        pred = np.array([R.grade_of(v) for v in (alpha + X @ beta)])
        out["grade_hit"] = round(float(np.mean(pred == np.array([int(g) for _, g, _ in gdata]))) * 100, 1)
    counts: dict[str, int] = {}
    for r in rows:
        for role in {ctx.role.get(k, "additive") for k in r["design"].whole()}:
            counts[role] = counts.get(role, 0) + 1
    for role in out["roles"]:
        out["roles"][role]["n"] = counts.get(role, 0)
    kb.write_yaml(R.CAL_PATH, out)
    return {"ok": True, **out}


def reset_calibration() -> None:
    if R.CAL_PATH.exists():
        R.CAL_PATH.unlink()


def compare() -> pd.DataFrame:
    """已回填的组：预测 vs 实测（用当前系数重新预测，看校准后对不对得上）。"""
    ctx = Ctx()
    cal = ctx.cal
    ref = base_design(DEFAULT_T_REF)
    rows = []
    for r in measured():
        d: Design = r["design"]
        if r.get("t_measured"):
            d = Design(a=d.a, b=d.b, a_ratio=d.a_ratio, thickness=float(r["t_measured"]))
        p = ctx.predict(d, ref)
        rows.append({"编号": r["code"], "配方": d.text(), "实测纵向N": r.get("md_n"), "预测纵向N": p.get("md_N"),
                     "实测横向N": r.get("td_n"), "预测横向N": p.get("td_N"),
                     "实测透光": R.GRADE_LABEL.get(r["grade"]) if r.get("grade") is not None else None,
                     "预测透光": p["grade_label"]})
    df = pd.DataFrame(rows)
    if not cal:
        df.attrs["note"] = "还没校准：预测纵/横向 N 要等现配方对照组回填后才有绝对值。"
    return df


# ---------------- 下一轮 ----------------

def next_round(query: dict[str, Any] | None = None, n_exploit: int = 6, n_explore: int = 3) -> dict[str, Any]:
    """用当前（已校准的）模型：每档最低成本的几组（利用）+ 离已做过的配方最远、但预测可行又便宜的几组（探索）。"""
    from . import optimize as O

    q = dict(query or {})
    q["top_k"] = max(int(q.get("top_k") or 0), 12)
    out = O.run(q)
    by_grade: dict[int, list[dict[str, Any]]] = {}
    for per in out["groups"].values():
        for g, items in per.items():
            by_grade.setdefault(g, []).extend(items)
    done = [r["design"] for r in trials()]
    done_keys = {d.key() for d in done}
    ctx = Ctx()

    def vec(d: Design) -> np.ndarray:
        return np.append(_features(ctx, d, R.ROLE_KEYS), [d.a_ratio * 10, d.thickness])

    chosen: list[tuple[Design, str]] = []
    keys: set = set(done_keys)

    def take(ev: dict[str, Any], why: str) -> bool:
        k = ev["design"].key()
        if k in keys:
            return False
        keys.add(k)
        chosen.append((ev["design"], why))
        return True

    # 利用：先保证每一档都有它预测最省的那组，再按成本补满
    for g in sorted(by_grade, reverse=True):
        for ev in sorted(by_grade[g], key=lambda e: e["cost"] or 1e9):
            if take(ev, f"利用：透光≥{R.GRADE_LABEL[g]} 预测最省"):
                break
    pool = sorted({ev["design"].key(): ev for items in by_grade.values() for ev in items}.values(),
                  key=lambda e: e["cost"] or 1e9)
    for ev in pool:
        if len(chosen) >= n_exploit:
            break
        take(ev, "利用：预测最省")
    # 探索：在预测可行的方案里，挑离已做过的配方最远的
    done_v = [vec(d) for d in done] + [vec(d) for d, _ in chosen]
    rest = [ev for ev in pool if ev["design"].key() not in keys]
    rest.sort(key=lambda e: -min((float(np.linalg.norm(vec(e["design"]) - v)) for v in done_v), default=0.0))
    for ev in rest[:n_explore]:
        take(ev, "探索：离已做过的远")
    items = list(chosen)
    items += [(base_design(q.get("t_ref", DEFAULT_T_REF)), "现配方对照")]
    rn, saved = _save_round(items, q.get("t_ref", DEFAULT_T_REF))
    path = export_round(rn, q.get("t_ref", DEFAULT_T_REF))
    return {"round": rn, "trials": saved, "path": str(path)}
