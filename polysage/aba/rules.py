"""定性性能模型（没有实验数据时的先验）：每类料每 10 个百分点（占整膜）对纵向/横向拉伸强度和透光分的影响。

- 拉伸强度指数 I = 1 + Σ 系数 × 占比/10，以纯 LLDPE 膜为 1；拉断力（N）= 强度 × 厚度，所以相对现配方的
  拉断力倍数 = (I / I_现配方) × (厚度 / 现配方厚度)。
- 透光分 S = 1.6 + Σ 系数 × 占比/10 − 0.12 × (厚度 − 现配方厚度)；S ≥ 1.5 → 2 很好，≥ 0.6 → 1 好，否则 0 一般。
- 系数表存在 knowledge/00_项目/ABA系数表.yaml，页面上可以改；实验数据回来后校准值另存 ABA系数表_校准.yaml，优先使用。
这些数都是先验（文献/经验），标了可信度，实验数据会把它们一条条改准。
"""
from __future__ import annotations

from typing import Any

from .. import kb
from ..config import KB

TABLE_PATH = KB["project"] / "ABA系数表.yaml"
CAL_PATH = KB["project"] / "ABA系数表_校准.yaml"

S0 = 1.6                    # 纯 LLDPE 膜在现配方厚度下的透光分
THICK_TRANS = -0.12         # 每厚 1 丝，透光分变化
GRADE_CUTS = (0.6, 1.5)     # 透光分 → 档位的分界
GRADE_LABEL = {0: "一般", 1: "好", 2: "很好"}

# 角色：md / td / trans 为每 10 个百分点（整膜占比）的影响；layers 为允许放的层；cap 为该层内最大用量 %
ROLES: dict[str, dict[str, Any]] = {
    "lldpe": {"label": "LLDPE（C4 线性）", "md": 0.00, "td": 0.00, "trans": 0.00, "density": 0.918,
              "layers": "AB", "cap": 100, "conf": "参考", "basis": "基准树脂"},
    "lldpe_c6": {"label": "C6/C8 LLDPE", "md": 0.04, "td": 0.05, "trans": 0.02, "density": 0.918,
                 "layers": "AB", "cap": 100, "conf": "中", "basis": "己烯/辛烯共聚，韧性和拉伸略好于 C4"},
    "mlldpe": {"label": "茂金属 LLDPE", "md": 0.08, "td": 0.10, "trans": 0.15, "density": 0.918,
               "layers": "AB", "cap": 30, "conf": "中", "basis": "分子量分布窄：强度与透明度都更好；熔体强度低，用量别太高"},
    "ldpe": {"label": "LDPE（高压新料）", "md": -0.04, "td": -0.03, "trans": 0.15, "density": 0.922,
             "layers": "AB", "cap": 30, "conf": "中", "basis": "长支链：拉伸强度降，光学与膜泡稳定性好"},
    "hdpe": {"label": "HDPE（低压）", "md": 0.06, "td": -0.02, "trans": -0.25, "density": 0.950,
             "layers": "B", "cap": 15, "conf": "中", "basis": "结晶度高：纵向强度与挺度升，横向易劈裂，雾度升"},
    "mdpe": {"label": "MDPE", "md": 0.03, "td": 0.00, "trans": -0.12, "density": 0.935,
             "layers": "B", "cap": 20, "conf": "低", "basis": "介于 LLDPE 与 HDPE 之间"},
    "recycled_ld": {"label": "高压再生回料", "md": -0.03, "td": -0.03, "trans": -0.10, "density": 0.922,
                    "layers": "AB", "cap": 60, "conf": "中", "basis": "近新料色的高压回料：强度略降，透光略降"},
    "recycled_ll": {"label": "线性再生料", "md": -0.02, "td": -0.02, "trans": -0.12, "density": 0.920,
                    "layers": "AB", "cap": 50, "conf": "低", "basis": "工业膜回料：强度接近，杂质影响透光"},
    "pcr": {"label": "消费后回料", "md": -0.06, "td": -0.07, "trans": -0.30, "density": 0.930,
            "layers": "B", "cap": 30, "conf": "低", "basis": "成分杂：强度与透光都差"},
    "filler": {"label": "碳酸钙填充母料", "md": -0.09, "td": -0.11, "trans": -0.55, "density": 1.95,
               "layers": "B", "cap": 50, "conf": "中", "basis": "无机填料：强度降（横向更明显），透光显著降；只放 B 层"},
    "clear_filler": {"label": "透明填充母料", "md": -0.06, "td": -0.07, "trans": -0.15, "density": 1.60,
                     "layers": "B", "cap": 30, "conf": "低", "basis": "细粒径/低折射率差填料：透光损失比碳酸钙小"},
    "talc": {"label": "滑石粉母料", "md": -0.05, "td": -0.08, "trans": -0.40, "density": 1.90,
             "layers": "B", "cap": 20, "conf": "低", "basis": "片状填料：挺度升，强度与透光降"},
    "elastomer": {"label": "弹性体（POE/POP/VLDPE）", "md": -0.02, "td": 0.02, "trans": 0.05, "density": 0.870,
                  "layers": "AB", "cap": 10, "conf": "低", "basis": "增韧不增强：横向略好，纵向略降"},
    "nucleator": {"label": "透明剂/成核剂", "md": 0.00, "td": 0.00, "trans": 3.0, "density": 1.00,
                  "layers": "AB", "cap": 1, "conf": "低", "basis": "千分之几的用量就能降雾度（系数按每 10% 折算，0.3% ≈ +0.09）"},
    "additive": {"label": "加工助剂母粒", "md": 0.00, "td": 0.00, "trans": 0.00, "density": 0.950,
                 "layers": "AB", "cap": 3, "conf": "中", "basis": "PPA/开口等，少量，对三项基本无影响"},
}
ROLE_KEYS = list(ROLES)


def classify(m: dict[str, Any]) -> str:
    """材料 → 角色。先认代码，再看名称/类别里的关键词。"""
    code = (m.get("code") or "").upper()
    name = f"{m.get('name') or ''} {m.get('grade') or ''}"
    cat = m.get("category") or ""
    fixed = {"LL": "lldpe", "LLC": "lldpe", "LL6": "lldpe_c6", "MLL": "mlldpe", "MLL8": "mlldpe", "LD": "ldpe",
             "HD": "hdpe", "MD": "mdpe", "R1": "recycled_ld", "R2": "recycled_ld", "RL": "recycled_ll", "PCR": "pcr",
             "RHD": "hdpe", "FL": "filler", "TFL": "clear_filler", "TALC": "talc", "POE": "elastomer",
             "POP": "elastomer", "VL": "elastomer", "NUC": "nucleator", "AD": "additive", "CP": "additive"}
    if code in fixed:
        return fixed[code]
    up = name.upper()
    if "透明剂" in name or "成核" in name:
        return "nucleator"
    if "透明填充" in name:
        return "clear_filler"
    if "填充" in name or "碳酸钙" in name or cat == "填充母料":
        return "filler"
    if "滑石" in name:
        return "talc"
    if "消费后" in name or "PCR" in up:
        return "pcr"
    if "再生" in name or "回料" in name or m.get("is_recycled"):
        return "recycled_ll" if ("线性" in name or "LLDPE" in up) else "recycled_ld"
    if "茂金属" in name or "MLLDPE" in up or "MPE" in up:
        return "mlldpe"
    if "HDPE" in up or "低压" in name:
        return "hdpe"
    if "MDPE" in up:
        return "mdpe"
    if "LLDPE" in up or "线性" in name:
        return "lldpe_c6" if ("己烯" in name or "辛烯" in name or "C6" in up or "C8" in up) else "lldpe"
    if "LDPE" in up or "高压" in name:
        return "ldpe"
    if "POE" in up or "POP" in up or "弹性体" in name or "VLDPE" in up:
        return "elastomer"
    if cat == "助剂":
        return "additive"
    return "lldpe" if cat == "主体树脂" else "additive"


def load_table(include_cal: bool = True) -> dict[str, dict[str, Any]]:
    """有效系数表：默认值 ← 用户改过的 ← 实验校准过的（后者优先）。校准时取先验要传 include_cal=False。"""
    table = {k: dict(v) for k, v in ROLES.items()}
    sources = ((TABLE_PATH, "user"), (CAL_PATH, "cal")) if include_cal else ((TABLE_PATH, "user"),)
    for path, tag in sources:
        over = kb.read_yaml(path) if path.exists() else {}
        for role, vals in (over.get("roles") or {}).items():
            if role not in table or not isinstance(vals, dict):
                continue
            for k in ("md", "td", "trans"):
                if isinstance(vals.get(k), (int, float)):
                    table[role][k] = float(vals[k])
            if tag == "cal":
                table[role]["calibrated"] = int(vals.get("n") or 0)
    return table


def calibration() -> dict[str, Any]:
    """校准结果（没有就是空）：base 拉断力绝对值、样本数、时间。"""
    return kb.read_yaml(CAL_PATH) if CAL_PATH.exists() else {}


def save_user_table(rows: dict[str, dict[str, float]]) -> None:
    """页面上改过的系数：只存和默认值不同的那几项。"""
    out: dict[str, dict[str, float]] = {}
    for role, vals in rows.items():
        if role not in ROLES:
            continue
        diff = {k: float(vals[k]) for k in ("md", "td", "trans")
                if k in vals and abs(float(vals[k]) - float(ROLES[role][k])) > 1e-9}
        if diff:
            out[role] = diff
    kb.write_yaml(TABLE_PATH, {"说明": "ABA 定性模型系数（每 10 个百分点整膜占比的影响），只记和默认值不同的项",
                               "roles": out})


def grade_of(score: float) -> int:
    return 2 if score >= GRADE_CUTS[1] else (1 if score >= GRADE_CUTS[0] else 0)
