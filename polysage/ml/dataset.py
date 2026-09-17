"""实验数据表（内部技术路线附录 A）：模板、读取、质检、按 sample_id 聚合、过关标签。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import KB
from ..formulation import constraints as C
from ..formulation.cost import CODE_ORDER

ID_COLS = ["sample_id", "date", "line", "operator", "layer_structure", "R1_batch", "RL_batch", "note"]
COMP_COLS = ["LL", "LLC", "LL6", "mLL", "mLL8", "LD", "HD", "MD", "R1", "R2", "RL", "PCR", "POE", "EVA", "FL", "TFL", "AD", "CP"]
PROC_COLS = ["thickness_um", "BUR", "melt_temp", "line_speed", "frost_line", "die_gap"]
# 四项红线（建模主目标）：拉伸、撕裂、穿刺、热封；其余为可选明细列（MD/TD、伸长、落镖、雾度…），填了会保留，不进过关判定
PRIMARY_TARGETS = ["tensile", "tear", "puncture", "seal"]
DETAIL_COLS = ["tensile_MD", "tensile_TD", "tear_MD", "tear_TD", "elong_MD", "elong_TD", "dart", "seal_Tm10", "seal_Tp10",
               "haze", "transmittance", "gel_count"]
TARGET_COLS = PRIMARY_TARGETS + DETAIL_COLS
HIGHER_BETTER = {**{k: True for k in PRIMARY_TARGETS}, "tensile_MD": True, "tensile_TD": True, "elong_MD": True, "elong_TD": True,
                 "tear_MD": True, "tear_TD": True, "dart": True, "seal_Tm10": True, "seal_Tp10": True, "haze": False,
                 "transmittance": True, "gel_count": False}
# source：base（现用膜）| scheme（按推荐方案试验）| own（自主实验）；scheme_code：对应配方库/方案编号
META_COLS = ["source", "scheme_code"]
ALL_COLS = ID_COLS[:5] + META_COLS + COMP_COLS + ID_COLS[5:7] + PROC_COLS + TARGET_COLS + ["pass_flag", ID_COLS[7]]

DATA_PATH = KB["experiment"] / "data.csv"
TEMPLATE_PATH = KB["experiment"] / "数据表模板.csv"


def write_template(path: Path | None = None) -> Path:
    path = path or TEMPLATE_PATH
    base = C.base_formulation()
    row = {c: 0 for c in COMP_COLS}
    row.update({k: v for k, v in base.items() if k in COMP_COLS})
    row.update({"sample_id": "S0-01", "layer_structure": "mono", "source": "base",
                "note": "示例：一行一次测量；四项主指标 tensile/tear/puncture/seal 必填（不分 MD/TD；若只有 MD/TD 明细，主指标可留空自动折算）"})
    df = pd.DataFrame([{c: row.get(c, "") for c in ALL_COLS}], columns=ALL_COLS)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def load(path: Path | None = None) -> pd.DataFrame:
    path = path or DATA_PATH
    if not path.exists():
        return pd.DataFrame(columns=ALL_COLS)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path, encoding="utf-8-sig")
    for c in COMP_COLS + PROC_COLS + TARGET_COLS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df[COMP_COLS] = df[COMP_COLS].fillna(0.0)
    df = derive_primary(df)
    for c in META_COLS:
        if c not in df.columns:
            df[c] = ""
    df["source"] = df["source"].fillna("").astype(str)
    df.loc[(df["source"] == "") & df["sample_id"].astype(str).str.upper().str.startswith("S0"), "source"] = "base"
    df.loc[df["source"] == "", "source"] = "own"
    return df


def derive_primary(df: pd.DataFrame) -> pd.DataFrame:
    """四项主指标缺失时从明细列折算：拉伸/撕裂取 MD、TD 的平均（只有一个就用那个）；热封取 seal（旧表 seal_T）。"""
    df = df.copy()
    for base_, md, td in (("tensile", "tensile_MD", "tensile_TD"), ("tear", "tear_MD", "tear_TD")):
        if base_ in df and md in df and td in df:
            fill = df[[md, td]].mean(axis=1, skipna=True)
            df[base_] = df[base_].where(df[base_].notna(), fill)
    if "seal" in df and "seal_T" in df.columns:
        df["seal"] = df["seal"].where(df["seal"].notna(), pd.to_numeric(df["seal_T"], errors="coerce"))
    return df


def save(df: pd.DataFrame, path: Path | None = None) -> Path:
    path = path or DATA_PATH
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def qc(df: pd.DataFrame) -> list[str]:
    """数据质检：合计 100、缺失、异常值、重复样、混厚度。"""
    issues: list[str] = []
    if df.empty:
        return ["数据表为空"]
    if "sample_id" not in df.columns or df["sample_id"].isna().any():
        issues.append("存在缺失 sample_id 的行")
    s = df[COMP_COLS].sum(axis=1)
    bad = df.loc[(s - 100).abs() > 0.5, "sample_id"].astype(str).tolist()
    if bad:
        issues.append(f"配方合计 ≠ 100：{', '.join(bad[:10])}")
    if "thickness_um" in df and df["thickness_um"].notna().any():
        th = df["thickness_um"].dropna()
        if th.nunique() > 1 and (th.max() - th.min()) / th.mean() > 0.15:
            issues.append(f"膜厚跨度 {th.min():g}～{th.max():g} μm > 15%：厚度对撕裂/穿刺影响比配方还大，建议分开建模")
    missing_primary = [t for t in PRIMARY_TARGETS if t in df and df[t].isna().all()]
    if missing_primary:
        issues.append(f"四项主指标缺列/全空：{', '.join(missing_primary)}（列名 tensile / tear / puncture / seal）")
    for t in PRIMARY_TARGETS:
        v = df[t].dropna()
        if len(v) >= 6:
            z = (v - v.mean()) / (v.std() + 1e-9)
            out = df.loc[v.index[z.abs() > 3], "sample_id"].astype(str).tolist()
            if out:
                issues.append(f"{t} 存在 |z|>3 的异常值：{', '.join(out[:5])}")
    if df["R1"].gt(0).any() and ("R1_batch" not in df or df.loc[df["R1"] > 0, "R1_batch"].isna().all()):
        issues.append("使用了再生料 R1 但未记录批号 R1_batch（批次差异会被模型学成配方效应）")
    return issues


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """按 sample_id 聚合：配方/工艺取首值，性能取均值与 SD、条数。"""
    if df.empty:
        return df
    first_cols = [c for c in ["layer_structure", "source", "scheme_code", "R1_batch", "RL_batch"] + COMP_COLS + PROC_COLS if c in df.columns]
    g = df.groupby("sample_id")
    out = g[first_cols].first()
    for t in TARGET_COLS:
        if t in df.columns:
            out[t] = g[t].mean()
            out[t + "_sd"] = g[t].std()
            out[t + "_n"] = g[t].count()
    return out.reset_index()


def base_stats(agg: pd.DataFrame, base_ids: list[str] | None = None) -> dict[str, dict[str, float]]:
    """base = 现用膜各项均值与 SD。优先取 00_项目/base.yaml（表单/对话录入）；否则取数据表里 sample_id 以 S0 开头的样。"""
    from .. import basedata

    yaml_stats = basedata.stats()
    if yaml_stats:
        return yaml_stats
    if agg is None or agg.empty:
        return {}
    mask = agg["sample_id"].astype(str).str.upper().str.startswith("S0") if base_ids is None else agg["sample_id"].isin(base_ids)
    b = agg[mask]
    if b.empty:
        return {}
    out = {}
    for t in TARGET_COLS:
        if t in b and b[t].notna().any():
            out[t] = {"mean": float(b[t].mean()), "sd": float(np.nanmean(b[t + "_sd"])) if (t + "_sd") in b else 0.0}
    return out


def thresholds(base: dict[str, dict[str, float]], rule: dict[str, Any] | None = None) -> dict[str, float]:
    """过关阈值：力学/热封 ≥ base×0.95 且 ≥ base−1SD → 取两者较大；雾度 ≤ base+1；透光率 ≥ base−1。"""
    rule = rule or C.load().get("pass_rule", {})
    ratio = float(rule.get("mechanical_ratio", 0.95))
    k_sd = float(rule.get("mechanical_sd", 1))
    thr: dict[str, float] = {}
    for t, s in base.items():
        if t not in PRIMARY_TARGETS:
            continue
        sd = s.get("sd") or 0.0
        thr[t] = max(s["mean"] * ratio, s["mean"] - k_sd * sd) if sd > 0 else s["mean"] * ratio
    return thr


def pass_flags(agg: pd.DataFrame, thr: dict[str, float], targets: list[str] | None = None) -> pd.Series:
    targets = [t for t in (targets or PRIMARY_TARGETS) if t in thr and t in agg]
    ok = pd.Series(True, index=agg.index)
    for t in targets:
        if HIGHER_BETTER.get(t, True):
            ok &= agg[t] >= thr[t]
        else:
            ok &= agg[t] <= thr[t]
    return ok.astype(int)


def merge_upload(new: pd.DataFrame, *, source: str, scheme_code: str | None = None, path: Path | None = None) -> tuple[pd.DataFrame, list[str]]:
    """把上传的一批测量并入 data.csv：补 source/scheme_code，校验，去重（同 sample_id + 同一行内容）。返回 (合并后表, 质检问题)。"""
    new = new.copy()
    for c in COMP_COLS + PROC_COLS + TARGET_COLS:
        if c not in new.columns:
            new[c] = np.nan
        new[c] = pd.to_numeric(new[c], errors="coerce")
    new[COMP_COLS] = new[COMP_COLS].fillna(0.0)
    new["source"] = source
    if scheme_code:
        new["scheme_code"] = scheme_code
    elif "scheme_code" not in new.columns:
        new["scheme_code"] = ""
    issues = qc(new)
    cur = load(path)
    merged = pd.concat([cur, new], ignore_index=True)
    key_cols = ["sample_id"] + TARGET_COLS
    merged = merged.drop_duplicates(subset=[c for c in key_cols if c in merged.columns], keep="last")
    save(merged, path)
    return merged, issues


def scheme_template(scheme_code: str, components: dict[str, float], n_samples: int = 3, thickness_um: float | None = None) -> pd.DataFrame:
    """按推荐方案预填配方列的数据表模板（每个样品一行占位，测量时按次数复制行）。"""
    rows = []
    for i in range(1, n_samples + 1):
        r = {c: "" for c in ALL_COLS}
        r.update({c: float(components.get(c, 0.0)) for c in COMP_COLS})
        r.update({"sample_id": f"{scheme_code}-{i:02d}", "layer_structure": "mono", "source": "scheme", "scheme_code": scheme_code,
                  "thickness_um": thickness_um or "", "note": "一行一次测量；同一样品多次测量复制本行"})
        rows.append(r)
    return pd.DataFrame(rows, columns=ALL_COLS)


def weigh_sheet(scheme_code: str, components: dict[str, float], batch_kg: float = 25.0, mats: dict[str, dict[str, Any]] | None = None) -> pd.DataFrame:
    """称料单：按批次公斤数把配方换算成每种料的用量。"""
    rows = []
    for k, pct in components.items():
        if not pct:
            continue
        m = (mats or {}).get(k, {})
        rows.append({"方案": scheme_code, "代码": k, "材料": m.get("name", k), "牌号": m.get("grade", ""), "比例%": pct,
                     "用量 kg": round(batch_kg * pct / 100, 2), "供应商": m.get("supplier", ""), "备注": ""})
    rows.append({"方案": scheme_code, "代码": "合计", "材料": "", "牌号": "", "比例%": round(sum(v for v in components.values() if v), 1),
                 "用量 kg": round(batch_kg, 2), "供应商": "", "备注": "同机台、同厚度、同工艺；稳定 20～30 分钟后取样"})
    return pd.DataFrame(rows)


def make_trial_kit(scheme_code: str, batch_kg: float = 25.0, n_samples: int = 3) -> list[Path]:
    """为配方库中的方案生成：称料单.xlsx + 预填数据表模板.csv（06_实验数据/方案试验包/）。"""
    from ..formulation.library import list_formulations
    from ..formulation.materials import list_materials

    f = next((x for x in list_formulations() if x["code"] == scheme_code), None)
    if not f:
        raise ValueError(f"配方库中没有 {scheme_code}")
    mats = {m["code"]: m for m in list_materials(active_only=False)}
    out_dir = KB["experiment"] / "方案试验包"
    out_dir.mkdir(parents=True, exist_ok=True)
    from .. import basedata

    th = basedata.load().get("thickness_um")
    tpl = scheme_template(scheme_code, f["components"], n_samples=n_samples, thickness_um=th)
    p1 = out_dir / f"{scheme_code}_数据表模板.csv"
    tpl.to_csv(p1, index=False, encoding="utf-8-sig")
    p2 = out_dir / f"{scheme_code}_称料单.xlsx"
    with pd.ExcelWriter(p2, engine="openpyxl") as xw:
        weigh_sheet(scheme_code, f["components"], batch_kg=batch_kg, mats=mats).to_excel(xw, sheet_name="称料单", index=False)
        pd.DataFrame({"要求": ["同机台、同目标膜厚、同线速、同吹胀比、同温度曲线；与 base 现用膜同条件对比",
                              "换料后排净过渡料，膜泡/压力/负荷稳定后再取样；稳定观察 ≥ 20～30 分钟，前/中/后三段各留样",
                              "再生料用同一批并记录批号（R1_batch / RL_batch）", "四项各 ≥ 5 条，记录单次测量值，不填平均",
                              "填好 数据表模板.csv 后在“实验与模型 → 推荐方案试验数据”上传"]}).to_excel(xw, sheet_name="执行要求", index=False)
    return [p1, p2]


def feature_frame(agg: pd.DataFrame, use_process: bool = False, mats: dict[str, dict[str, Any]] | None = None) -> pd.DataFrame:
    """建模输入：配方列 + （可选）材料描述符加权均值（MFR、密度、再生比例）+ 工艺列。"""
    X = agg[[c for c in COMP_COLS if c in agg]].copy().fillna(0.0)
    if mats:
        w = X.values / 100.0
        dens = np.array([(mats.get(c) or {}).get("density") or 0.92 for c in X.columns])
        mfi = np.array([(mats.get(c) or {}).get("mfi") or 1.5 for c in X.columns])
        rec = np.array([1.0 if (mats.get(c) or {}).get("is_recycled") else 0.0 for c in X.columns])
        X["desc_density"] = w @ dens
        X["desc_mfi"] = w @ mfi
        X["desc_recycled"] = w @ rec
    if use_process:
        for c in PROC_COLS:
            if c in agg and agg[c].notna().any():
                X[c] = agg[c].fillna(agg[c].median())
    return X
