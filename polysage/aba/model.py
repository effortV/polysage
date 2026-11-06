"""ABA 膜的表示、成本与定性预测。

一个方案 = A 层配方 + B 层配方 + A 层占比（两面合计）+ 厚度（丝）。
成本按吨：a × A 层单价 + (1 − a) × B 层单价；克重 g/m² 只作参考（填充料会把密度拉高）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from . import rules as R

BASE_RESIN = "LL"          # 现配方的 LLDPE
BASE_RECYCLED = "R1"       # 现配方的高压再生回料
BASE_SPLIT = {BASE_RESIN: 66.7, BASE_RECYCLED: 33.3}     # 50 kg LLDPE + 25 kg 再生
BASE_A_RATIO = 0.45
DEFAULT_T_REF = 4.5        # 现配方对照厚度（丝）；常用 3~6 丝
SAFE_Z = 0.84              # 推荐时透光按“八成把握”承诺：分数减 0.84 个标准差再判档
PRIOR_TRANS_SD = 0.15      # 没被实验校准过的料：每 10 个百分点，透光分 ±0.15 的不确定


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def layer_text(comp: dict[str, float]) -> str:
    return " / ".join(f"{k} {_fmt(v)}" for k, v in sorted(comp.items(), key=lambda kv: -kv[1]) if v > 1e-9)


@dataclass
class Design:
    a: dict[str, float]                 # A 层（表层，两面同配方）组分 %
    b: dict[str, float]                 # B 层（芯层）组分 %
    a_ratio: float = BASE_A_RATIO       # A 层占整膜的比例
    thickness: float = DEFAULT_T_REF    # 丝（1 丝 = 10 μm）
    tags: list[str] = field(default_factory=list)

    def whole(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for k, v in self.a.items():
            out[k] = out.get(k, 0.0) + v * self.a_ratio
        for k, v in self.b.items():
            out[k] = out.get(k, 0.0) + v * (1 - self.a_ratio)
        return {k: round(v, 3) for k, v in out.items() if v > 1e-9}

    def text(self) -> str:
        return (f"A {self.a_ratio * 100:.0f}%：{layer_text(self.a)} ｜ B {(1 - self.a_ratio) * 100:.0f}%：{layer_text(self.b)}"
                f" ｜ {_fmt(self.thickness)} 丝")

    def key(self) -> tuple:
        r = lambda d: tuple(sorted((k, round(v, 1)) for k, v in d.items() if v > 1e-9))  # noqa: E731
        return (r(self.a), r(self.b), round(self.a_ratio, 3), round(self.thickness, 2))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Design":
        return cls(a={k: float(v) for k, v in (d.get("a") or {}).items()}, b={k: float(v) for k, v in (d.get("b") or {}).items()},
                   a_ratio=float(d.get("a_ratio", BASE_A_RATIO)), thickness=float(d.get("thickness", DEFAULT_T_REF)),
                   tags=list(d.get("tags") or []))


def base_design(t_ref: float = DEFAULT_T_REF, *, filler: float = 0.0, hdpe: float = 0.0, a_ratio: float = BASE_A_RATIO,
                filler_code: str = "FL", hdpe_code: str = "HD") -> Design:
    """现配方：A、B 共用基底 LLDPE 67 / 再生 33；B 层可带填充与 HDPE（按实际情况填）。"""
    b = {k: v * (100 - filler - hdpe) / 100 for k, v in BASE_SPLIT.items()}
    if filler:
        b[filler_code] = filler
    if hdpe:
        b[hdpe_code] = hdpe
    return Design(a=dict(BASE_SPLIT), b=b, a_ratio=a_ratio, thickness=t_ref, tags=["现配方"])


def current_price_map() -> dict[str, float]:
    """每种料按生效价（实价优先；实价过期时用更新的网查价）。"""
    from ..formulation.materials import effective_prices

    return {code: e["price"] for code, e in effective_prices().items()}


class Ctx:
    """一次计算用的材料上下文：角色、密度、价格、系数表。寻优时会评估上万个方案，所以只查一次库。"""

    def __init__(self, prices: dict[str, float] | None = None, table: dict[str, dict[str, Any]] | None = None):
        from ..formulation.materials import list_materials

        self.mats = {m["code"]: m for m in list_materials(active_only=False)}
        self.role = {c: R.classify(m) for c, m in self.mats.items()}
        self.table = table or R.load_table()
        self.prices = dict(prices) if prices is not None else current_price_map()
        self.dens = {c: float(m.get("density") or R.ROLES[self.role[c]]["density"]) for c, m in self.mats.items()}
        self.cal = R.calibration()
        self.s0 = float(self.cal.get("trans_intercept", R.S0))          # 校准后用数据给的透光基线
        self.kt = float(self.cal.get("thick_trans", R.THICK_TRANS))     # 每厚 1 丝透光分变化
        # 校准给的透光参数协方差：推荐时按“至少八成把握达到”的档位承诺，拿不准的不往高档里报
        self.t_terms = list(self.cal.get("trans_terms") or [])
        cov = self.cal.get("trans_cov")
        self.t_cov = None
        if cov and len(cov) == len(self.t_terms):
            import numpy as _np

            self.t_cov = _np.array(cov, dtype=float)

    # ---------- 成本与密度 ----------
    def layer_cost(self, comp: dict[str, float]) -> float | None:
        total = 0.0
        for k, v in comp.items():
            if v <= 0:
                continue
            p = self.prices.get(k)
            if p is None:
                return None
            total += p * v / 100
        return total

    def cost(self, d: Design) -> float | None:
        ca, cb = self.layer_cost(d.a), self.layer_cost(d.b)
        if ca is None or cb is None:
            return None
        return d.a_ratio * ca + (1 - d.a_ratio) * cb

    def missing_prices(self, d: Design) -> list[str]:
        return sorted({k for k in list(d.a) + list(d.b) if (d.a.get(k, 0) or d.b.get(k, 0)) and k not in self.prices})

    def _layer_density(self, comp: dict[str, float]) -> float:
        inv = sum((v / 100) / self.dens.get(k, 0.92) for k, v in comp.items() if v > 0)
        return 1 / inv if inv else 0.92

    def density(self, d: Design) -> float:
        inv = d.a_ratio / self._layer_density(d.a) + (1 - d.a_ratio) / self._layer_density(d.b)
        return 1 / inv

    def gsm(self, d: Design) -> float:
        """克重 g/m²：密度 × 厚度（1 丝 = 10 μm，1 m² × 1 μm = 1 cm³）。"""
        return self.density(d) * d.thickness * 10

    # ---------- 定性预测 ----------
    def indices(self, d: Design) -> tuple[float, float, float]:
        """强度指数取乘法形式 exp(Σ 系数 × 占比/10)：小用量时等于“每 10 点变化百分之几”，大用量时不会算成负数，
        也和校准时的对数线性拟合一致。透光分是线性加和。"""
        md = td = 0.0
        tr = self.s0
        for k, share in d.whole().items():
            c = self.table[self.role.get(k, "additive")]
            md += c["md"] * share / 10
            td += c["td"] * share / 10
            tr += c["trans"] * share / 10
        return math.exp(md), math.exp(td), tr

    def trans_sd(self, d: Design) -> float:
        """透光分预测的标准差：校准过的料用数据给的协方差；没校准过的料按先验，每 10 点 ±0.15 的不确定。"""
        if self.t_cov is None:
            return 0.0
        shares: dict[str, float] = {}
        for k, share in d.whole().items():
            role = self.role.get(k, "additive")
            shares[role] = shares.get(role, 0.0) + share / 10
        x = [1.0] + [shares.pop(r, 0.0) for r in self.t_terms[1:-1]] + [d.thickness - DEFAULT_T_REF]
        import numpy as _np

        xv = _np.array(x)
        var = float(xv @ self.t_cov @ xv)
        var += sum((v * PRIOR_TRANS_SD) ** 2 for r, v in shares.items() if r != "lldpe")
        return max(var, 0.0) ** 0.5

    def grade_safe(self, score: float, d: Design) -> int:
        """承诺的档位：透光分减去 0.84 个标准差（≈ 八成把握）再判档。没校准时就是点估计。"""
        return R.grade_of(score - SAFE_Z * self.trans_sd(d))

    def predict(self, d: Design, ref: Design) -> dict[str, Any]:
        md, td, tr = self.indices(d)
        md0, td0, _ = self.indices(ref)
        md_rel = (md / md0) * (d.thickness / ref.thickness)
        td_rel = (td / td0) * (d.thickness / ref.thickness)
        score = tr + self.kt * (d.thickness - ref.thickness)
        point = R.grade_of(score)
        grade = self.grade_safe(score, d)
        out = {"md_rel": round(md_rel, 3), "td_rel": round(td_rel, 3), "trans_score": round(score, 2),
               "grade": grade, "grade_label": R.GRADE_LABEL[grade], "grade_point": point,
               "trans_sd": round(self.trans_sd(d), 3), "confidence": self.confidence(d)}
        if self.cal.get("md_N_ref"):
            out["md_N"] = round(md_rel * float(self.cal["md_N_ref"]), 1)
        if self.cal.get("td_N_ref"):
            out["td_N"] = round(td_rel * float(self.cal["td_N_ref"]), 1)
        return out

    def drivers(self, d: Design, ref: Design, top: int = 4) -> list[str]:
        """哪几种料、在哪层、多少用量，主要推动了预测——给人看的依据。"""
        w, w0 = d.whole(), ref.whole()
        rows = []
        for k in set(w) | set(w0):
            delta = w.get(k, 0.0) - w0.get(k, 0.0)
            if abs(delta) < 0.5:
                continue
            role = self.role.get(k, "additive")
            c = self.table[role]
            where = "B 层" if k not in d.a and k in d.b else ("A 层" if k in d.a and k not in d.b else "两层")
            rows.append((abs(c["md"] * delta) + abs(c["td"] * delta) + abs(c["trans"] * delta) / 5,
                         f"{k}（{c['label']}，{where}）{'+' if delta > 0 else ''}{delta:.1f} 点：纵 {math.exp(c['md'] * delta / 10) - 1:+.0%}，"
                         f"横 {math.exp(c['td'] * delta / 10) - 1:+.0%}，透光分 {c['trans'] * delta / 10:+.2f}——{c['basis']}"))
        if abs(d.thickness - ref.thickness) > 1e-9:
            r = d.thickness / ref.thickness - 1
            rows.append((abs(r), f"厚度 {_fmt(ref.thickness)} → {_fmt(d.thickness)} 丝：拉断力 {r:+.0%}（与厚度成正比），"
                                 f"透光分 {self.kt * (d.thickness - ref.thickness):+.2f}"))
        return [t for _, t in sorted(rows, reverse=True)[:top]]

    def confidence(self, d: Design) -> str:
        low = 0
        for k in d.whole():
            c = self.table[self.role.get(k, "additive")]
            if c.get("conf") == "低" and not c.get("calibrated"):
                low += 1
        filler_b = sum(v for k, v in d.b.items() if self.role.get(k) in ("filler", "talc", "clear_filler"))
        if filler_b > 40:
            low += 1
        if not (3.0 <= d.thickness <= 6.0):
            low += 1
        n_cal = int(self.cal.get("n") or 0)
        if n_cal >= 12 and low == 0:
            return "高"
        return "中" if low == 0 else ("中低" if low == 1 else "低")

    def check(self, d: Design) -> list[str]:
        """结构约束：每层合计 100、料只能进允许的层、层内用量不超上限、厚度在 2~8 丝。"""
        issues = []
        for name, comp in (("A", d.a), ("B", d.b)):
            s = sum(comp.values())
            if abs(s - 100) > 0.6:
                issues.append(f"{name} 层合计 {s:.1f}% ≠ 100%")
            for k, v in comp.items():
                if v <= 0:
                    continue
                role = self.table[self.role.get(k, "additive")]
                if name not in role["layers"]:
                    issues.append(f"{k}（{role['label']}）只能放 {'/'.join(role['layers'])} 层")
                if v > role["cap"] + 1e-9:
                    issues.append(f"{name} 层 {k} {v:g}% 超过上限 {role['cap']}%")
        if not (0.2 <= d.a_ratio <= 0.6):
            issues.append(f"A 层占比 {d.a_ratio:.0%} 不在 20%~60%")
        if not (2.0 <= d.thickness <= 8.0):
            issues.append(f"厚度 {d.thickness:g} 丝不在 2~8 丝")
        return issues

    def evaluate(self, d: Design, ref: Design) -> dict[str, Any]:
        cost = self.cost(d)
        gsm = self.gsm(d)
        return {"design": d, "text": d.text(), "cost": None if cost is None else round(cost, 1),
                "gsm": round(gsm, 1), "cost_m2": None if cost is None else round(cost * gsm / 1e6, 4),   # 元/吨 × g/m² → 元/m²
                "issues": self.check(d), "missing": self.missing_prices(d), **self.predict(d, ref)}
