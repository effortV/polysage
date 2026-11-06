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
    xlsx = export_round(rn, t_ref)
    docx = export_round_docx(rn, t_ref)
    return {"round": rn, "trials": saved, "path": str(docx), "xlsx": str(xlsx), "docx": str(docx)}


# ---------------- 一整轮：对照 + 当前价最省 + 新料 + D-最优铺开 ----------------

# 因素水平：A 层再生（表层，不超过现配方 33.3%）、B 层再生、B 层填充、B 层 HDPE、A 层占比、厚度
SPACE = {"r_a": (0.0, 20.0, 33.3), "r_b": (20.0, 33.3, 45.0, 55.0), "f": (0.0, 10.0, 20.0, 30.0, 40.0, 50.0),
         "h": (0.0, 5.0, 10.0), "a": (0.35, 0.40, 0.45, 0.50), "t": (3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0)}
MIN_REL = 0.7          # 建模组也不做太离谱的：预测拉断力至少是现配方的 70%（太薄太稀的膜吹不起来）


def compose(r_a: float, r_b: float, f: float, h: float, a: float, t: float,
            resin: str = "LL", recycled: str = "R1", filler: str = "FL", hdpe: str = "HD") -> Design:
    a_comp = {resin: round(100 - r_a, 2), recycled: round(r_a, 2)}
    keep = (100 - f - h) / 100
    b_comp = {resin: round((100 - r_b) * keep, 2), recycled: round(r_b * keep, 2)}
    if f:
        b_comp[filler] = f
    if h:
        b_comp[hdpe] = h
    b_comp = {k: v for k, v in b_comp.items() if v > 1e-9}
    return Design(a={k: v for k, v in a_comp.items() if v > 1e-9}, b=b_comp, a_ratio=a, thickness=t)


def _factor_vec(r_a, r_b, f, h, a, t) -> np.ndarray:
    """缩放到 [-1, 1]，再展开成 1 + 一次 + 平方（≥3 个水平的因素）+ 两两交互：首轮多做几组，留出看弯曲和交互的余地。"""
    lo_hi = {k: (min(v), max(v)) for k, v in SPACE.items()}
    x = []
    for k, v in zip(SPACE, (r_a, r_b, f, h, a, t)):
        lo, hi = lo_hi[k]
        x.append(0.0 if hi == lo else 2 * (v - lo) / (hi - lo) - 1)
    x = np.array(x)
    sq = [x[i] ** 2 for i, k in enumerate(SPACE) if len(SPACE[k]) >= 3]
    inter = [x[i] * x[j] for i in range(len(x)) for j in range(i + 1, len(x))]
    return np.concatenate([[1.0], x, sq, inter])


def _design_factors(d: Design) -> tuple[float, ...] | None:
    """从配方反推六个因素（只认现有四种料组成的配方）；认不出就返回 None。"""
    if set(d.a) - {"LL", "R1"} or set(d.b) - {"LL", "R1", "FL", "HD"}:
        return None
    f, h = d.b.get("FL", 0.0), d.b.get("HD", 0.0)
    base = d.b.get("LL", 0.0) + d.b.get("R1", 0.0)
    r_b = 100 * d.b.get("R1", 0.0) / base if base else 0.0
    return (d.a.get("R1", 0.0), r_b, f, h, d.a_ratio, d.thickness)


def _augment(F_fixed: np.ndarray, F_pool: np.ndarray, n: int, iters: int = 20) -> list[int]:
    """在已定的组（对照、最省方案）基础上，再从候选池挑 n 组使信息矩阵行列式最大（Fedorov 交换，行列式引理加速）。"""
    p = F_pool.shape[1]
    M = (F_fixed.T @ F_fixed if len(F_fixed) else np.zeros((p, p))) + 1e-6 * np.eye(p)
    sel: list[int] = []
    for _ in range(min(n, len(F_pool))):
        Minv = np.linalg.inv(M)
        var = np.einsum("ij,jk,ik->i", F_pool, Minv, F_pool)
        var[sel] = -np.inf
        j = int(np.argmax(var))
        sel.append(j)
        M = M + np.outer(F_pool[j], F_pool[j])
    for _ in range(iters):
        improved = False
        for k in range(len(sel)):
            xo = F_pool[sel[k]]
            Mm = M - np.outer(xo, xo)
            Minv = np.linalg.inv(Mm)
            var = np.einsum("ij,jk,ik->i", F_pool, Minv, F_pool)
            old = float(xo @ Minv @ xo)
            var[sel] = -np.inf
            j = int(np.argmax(var))
            if var[j] > old * 1.0001 + 1e-9:
                sel[k] = j
                M = Mm + np.outer(F_pool[j], F_pool[j])
                improved = True
        if not improved:
            break
    return sel


def plan_round(n_total: int = 50, *, t_ref: float = DEFAULT_T_REF, scenario: str = "current", n_exploit: int = 12,
               n_new: int = 5, md_req: float | None = 1.0, td_req: float | None = 1.0, batch_kg: float = 100.0) -> dict[str, Any]:
    """一整轮试验（默认 50 组）：
    1. 现配方对照：3.5 / 4.5 / 5.5 丝各一组——拉断力的绝对值、厚度效应；
    2. 降本候选：按选定的价格，三个透光档各自预测最省、拉断力不低于现配方的方案（只用现有四种料）；
    3. 新料候选：库里买得到的新料里，预测比只用现有料更省的（要先确认料源与外观）；
    4. 建模组：其余名额用 D-最优在六个因素上铺开（含平方与交互），让回填后的机器学习能把系数、弯曲、交互学出来。
    """
    from . import optimize as O
    from .prices import build as build_prices

    ctx = Ctx(prices=build_prices(scenario)["prices"])
    ref = base_design(t_ref)
    items: list[tuple[Design, str]] = []
    keys: set = {r["design"].key() for r in trials()}

    def add(d: Design, why: str) -> bool:
        k = d.key()
        if k in keys or len(items) >= n_total:
            return False
        keys.add(k)
        items.append((d, why))
        return True

    for t in (3.5, 4.5, 5.5):
        add(base_design(t), "现配方对照")
    ref_cost = ctx.cost(ref)
    q = {"md_req": md_req, "td_req": td_req, "scenario": scenario, "t_ref": t_ref, "top_k": 8}
    cur = O.run({**q, "use_new": False})
    per = cur["groups"].get("现有料", {})
    want = max(1, math.ceil(n_exploit / max(1, len(per))))
    for g in sorted(per, reverse=True):
        got = 0
        for ev in per[g]:
            n_done = len([1 for _, w in items if w.startswith(("降本", "提透光"))])
            cheaper = bool(ref_cost and ev["cost"] < ref_cost - 1)
            if got >= (want if cheaper else min(2, want)) or n_done >= n_exploit:
                break
            pct = abs(ref_cost - ev["cost"]) / ref_cost * 100 if ref_cost else 0.0
            why = (f"降本：透光≥{R.GRADE_LABEL[g]} 预测最省（{ev['cost']:.0f} 元/吨，省 {pct:.1f}%）" if cheaper else
                   f"提透光：透光{R.GRADE_LABEL[g]}里最省（{ev['cost']:.0f} 元/吨，比现配方贵 {pct:.1f}%）")
            if add(ev["design"], why):
                got += 1
    if n_new:
        new = O.run({**q, "use_new": True})
        per_new = {g: sorted(items_, key=lambda e: e["cost"] or 1e9) for g, items_ in (new["groups"].get("加新料") or {}).items()}
        k_new, rank = 0, 0
        while k_new < n_new and any(len(v) > rank for v in per_new.values()):
            for g in sorted(per_new, reverse=True):                 # 每档轮流挑：先很好、再好、再一般
                if k_new >= n_new or rank >= len(per_new[g]):
                    continue
                ev = per_new[g][rank]
                mats = sorted(set(ev["design"].whole()) - O.CURRENT_CODES)
                names = "、".join(f"{c} {ctx.mats.get(c, {}).get('name', '')}".strip() for c in mats)
                pct = (ref_cost - ev["cost"]) / ref_cost * 100 if ref_cost else 0.0
                if add(ev["design"], f"新料：透光≥{R.GRADE_LABEL[g]}，用 {names}（{ev['cost']:.0f} 元/吨，省 {pct:.1f}%；先确认料源与外观）"):
                    k_new += 1
            rank += 1
    # 建模组：D-最优补满
    pool, F = [], []
    for r_a in SPACE["r_a"]:
        for r_b in SPACE["r_b"]:
            for f in SPACE["f"]:
                for h in SPACE["h"]:
                    for a in SPACE["a"]:
                        for t in SPACE["t"]:
                            d = compose(r_a, r_b, f, h, a, t)
                            if d.key() in keys or ctx.check(d):
                                continue
                            p = ctx.predict(d, ref)
                            if p["md_rel"] < MIN_REL or p["td_rel"] < MIN_REL:
                                continue
                            pool.append(d)
                            F.append(_factor_vec(r_a, r_b, f, h, a, t))
    fixed = [_factor_vec(*fx) for d, _ in items if (fx := _design_factors(d)) is not None]
    n_fill = n_total - len(items)
    if n_fill > 0 and pool:
        sel = _augment(np.array(fixed) if fixed else np.zeros((0, len(F[0]))), np.array(F), n_fill)
        picked = sorted((pool[j] for j in sel), key=lambda d: (d.a.get("R1", 0), d.b.get("HD", 0), d.b.get("FL", 0),
                                                                d.b.get("R1", 0), d.a_ratio, d.thickness))
        for d in picked:
            add(d, "建模：铺开六个因素")
    rn, saved = _save_round(items, t_ref)
    meta = {"scenario": cur["scenario"], "query": cur["query"], "notes": cur["notes"], "n_pool": len(pool),
            "counts": {}}
    for _, why in items:
        kind = why.split("：")[0]
        meta["counts"][kind] = meta["counts"].get(kind, 0) + 1
    kb.write_yaml(KB["experiment"] / f"round_{rn}_meta.yaml", meta)
    xlsx = export_round(rn, t_ref, batch_kg)
    docx = export_round_docx(rn, t_ref, batch_kg)
    return {"round": rn, "trials": saved, "path": str(docx), "xlsx": str(xlsx), "docx": str(docx), "meta": meta}


def round_meta(round_no: int) -> dict[str, Any]:
    p = KB["experiment"] / f"round_{round_no}_meta.yaml"
    return kb.read_yaml(p) if p.exists() else {}


# ---------------- Word：试验方案 + 投料单 + 数据记录表（填好可以直接上传回来）----------------

RECORD_HEADER = ["编号", "设定厚度(丝)", "实测厚度(丝)", "纵向拉断力(N)", "横向拉断力(N)", "透光档(0一般/1好/2很好)", "备注"]


def export_round_docx(round_no: int, t_ref: float = DEFAULT_T_REF, batch_kg: float = 100.0) -> Path:
    from .. import wordout as W
    from .prices import price_row

    rows = trials(round_no)
    meta = round_meta(round_no)
    ctx = Ctx()
    ref = base_design(t_ref)
    ref_cost = ctx.cost(ref)
    doc = W.new_doc(f"ABA 三层膜 · 第 {round_no} 轮试验方案（{len(rows)} 组）",
                    f"对照厚度 {t_ref:g} 丝　·　价格：{(meta.get('scenario') or {}).get('label', '当前价格卡')}", landscape=True)
    doc.add_heading("一、这一轮怎么安排", level=1)
    counts = meta.get("counts") or {}
    explain = {
        "现配方对照": "现配方在 3.5 / 4.5 / 5.5 丝各一组：拿到拉断力的绝对值（N）和厚度效应，所有预测都以它为基准。",
        "降本": "按当前价格，各透光档里预测比现配方便宜、且纵横拉断力不低于现配方的方案——最可能直接用上的。",
        "提透光": "要“很好”透光就得少用或不用表层再生料，每吨会比现配方贵；这几组用来确认值不值。",
        "新料": "库里网上找到、买得到的新料，预测比只用现有料更省；打样前先确认料源、外观（本色/透明）和稳定供货。",
        "建模": "D-最优设计在 A/B 层再生比例、B 层填充、B 层 HDPE、A 层占比、厚度六个因素上铺开，"
                "数据回来后机器学习（岭回归 + 有序回归）才能把每个因素的影响、弯曲和交互学准。",
        "利用": "按已用实测校准过的模型，各透光档预测最省、拉断力达标的几组——最可能直接用上的。",
        "探索": "预测可行、但离已做过的配方最远的几组：补数据空白，让模型在没试过的区域也准。",
    }
    if not counts:                                   # 下一轮等没记 meta 的：按目的的前缀数
        for r in rows:
            kind = r["purpose"].split("：")[0]
            counts[kind] = counts.get(kind, 0) + 1
    if counts.get("现配方对照") == 1:
        explain["现配方对照"] = "现配方 4.5 丝一组，每轮都做：看机台、原料批次有没有漂移，所有预测都以它为基准。"
    for k, n in counts.items():
        W.para(doc, f"**{k} {n} 组**" + (f"：{explain[k]}" if k in explain else ""), style="List Bullet")
    W.note(doc, ("顺序建议：先做现配方对照和降本组（最有用），建模组已按配方排好，同一配方连着改厚度最省换料时间。"
                 if "建模" in counts else "顺序建议：先做现配方对照，再做利用组，最后做探索组。")
           + "做不了的组（吹不起来、外观明显不行）在备注里写原因，同样有用。")

    doc.add_heading("二、试验配方", level=1)
    plan = []
    for r in rows:
        d, p = r["design"], r["predicted"]
        cost = p.get("cost")
        pct = (ref_cost - cost) / ref_cost * 100 if cost and ref_cost else None
        plan.append({"编号": r["code"], "目的": r["purpose"], "A 层（占比）": f"{d.a_ratio:.0%}：{layer_text(d.a)}",
                     "B 层（占比）": f"{1 - d.a_ratio:.0%}：{layer_text(d.b)}", "厚度(丝)": d.thickness,
                     "预计元/吨": cost, "较现配方": "" if pct is None else ("持平" if abs(pct) < 0.05 else
                                                                         (f"省 {pct:.1f}%" if pct > 0 else f"贵 {-pct:.1f}%")),
                     "预测纵/横(×现配方)": f"{p.get('md_rel', 0):.2f} / {p.get('td_rel', 0):.2f}", "预测透光": p.get("grade_label")})
    W.add_table(doc, plan, widths_cm=[1.4, 5.2, 4.6, 5.4, 1.3, 1.6, 1.5, 2.4, 1.3], font_size=8)
    W.note(doc, f"现配方（{t_ref:g} 丝）：{ref.text()}，{ref_cost:.0f} 元/吨。“较现配方”按每吨价格比；预测拉断力是相对现配方 "
                f"{t_ref:g} 丝的倍数，回填校准后会换成 N。" if ref_cost else "现配方缺价。")

    doc.add_heading(f"三、投料单（每组按 {batch_kg:g} kg 成品折算）", level=1)
    kg_rows = []
    for r in rows:
        kg = _kg(r["design"], batch_kg)
        kg_rows.append({"编号": r["code"], **{k.replace(" kg", ""): v for k, v in kg.items()}})
    cols = ["编号"] + sorted({k for row in kg_rows for k in row if k != "编号"}, key=lambda c: (c[:2], c))
    W.add_table(doc, (cols, [[row.get(c, "") for c in cols] for row in kg_rows]), font_size=8)
    W.note(doc, "A 层两面同配方，kg 是两面合计。代码含义见“六、用到的料和价格”。")

    doc.add_heading("四、测试与判档约定", level=1)
    W.bullets(doc, TEST_NOTE)

    doc.add_heading("五、数据记录表（填好后在「实验与校准 › 回填实测」上传这个 Word 即可）", level=1)
    W.add_table(doc, (RECORD_HEADER, [[r["code"], r["design"].thickness, "", "", "", "", ""] for r in rows]),
                widths_cm=[1.6, 2.0, 2.2, 3.0, 3.0, 3.4, 9.0], font_size=9, row_height_cm=0.7)

    doc.add_heading("六、用到的料和价格", level=1)
    used = sorted({k for r in rows for k in r["design"].whole()})
    price_rows = []
    for c in used:
        pr = price_row(c) or {}
        price_rows.append({"代码": c, "名称": ctx.mats.get(c, {}).get("name", ""), "单价(元/吨)": ctx.prices.get(c),
                           "报价日期": pr.get("price_date") or "", "来源": (pr.get("source") or "")[:60]})
    W.add_table(doc, price_rows, widths_cm=[1.4, 6, 2.2, 2.4, 12], font_size=8.5)
    for n in (meta.get("notes") or [])[:4]:
        W.note(doc, "· " + n)
    path = KB["experiment"] / f"ABA第{round_no}轮试验方案_{date.today().isoformat()}.docx"
    return W.save(doc, path)


def _docx_record_frame(source: Any) -> pd.DataFrame:
    from .. import wordout as W

    for t in W.read_tables(source):
        if not t:
            continue
        head = t[0]
        if any("编号" in h for h in head) and any("纵向" in h for h in head):
            return pd.DataFrame([r[:len(head)] for r in t[1:]], columns=head)
    raise ValueError("Word 里找不到数据记录表（表头要有“编号”和“纵向拉断力”）")


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
    """读回填好的数据记录表（Word 方案 / xlsx 方案 / DataFrame），按编号写回。"""
    name = str(getattr(source, "name", source if isinstance(source, (str, Path)) else "")).lower()
    if isinstance(source, pd.DataFrame):
        df = source
    elif name.endswith(".docx"):
        df = _docx_record_frame(source)                 # 填好的 Word 方案直接传回来
    else:
        df = pd.read_excel(source, sheet_name="数据记录")
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
    # 拉普拉斯近似：在最优点用梯度差分求海森矩阵，逆就是参数的（近似）协方差——数据少、离分界远的料，方差就大
    th, h = res.x, 1e-4
    H = np.zeros((len(th), len(th)))
    for j in range(len(th)):
        e = np.zeros(len(th))
        e[j] = h
        H[:, j] = (objective(th + e)[1] - objective(th - e)[1]) / (2 * h)
    H = (H + H.T) / 2
    try:
        cov = np.linalg.inv(H)
        cov = (cov + cov.T) / 2
    except np.linalg.LinAlgError:
        cov = np.eye(len(th)) * 0.05
    _ordinal_to_prior.last_cov = cov
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
        out["trans_terms"] = ["_intercept"] + used + ["_thick"]            # 协方差的行列顺序
        out["trans_cov"] = [[round(float(v), 6) for v in row] for row in _ordinal_to_prior.last_cov]
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
                     "预测透光": R.GRADE_LABEL[p.get("grade_point", p["grade"])]})
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
    # 探索一：透光拿不准的——点估计能上一档、但还没有八成把握；测了就知道这档能不能用更便宜的配方做到
    n_far = n_explore
    unsure = list((out.get("uncertain") or {}).values())             # 某档没有八成把握时，点估计最省的那组
    unsure += sorted((ev for ev in pool if ev["design"].key() not in keys and ev.get("grade_point", ev["grade"]) > ev["grade"]),
                     key=lambda e: e["cost"] or 1e9)
    unsure = [ev for ev in unsure if ev["design"].key() not in keys]
    for ev in unsure[:min(2, max(0, n_explore - 1))]:
        if take(ev, f"探索：透光拿不准（可能到“{R.GRADE_LABEL[ev['grade_point']]}”），测了能确认"):
            n_far -= 1
    # 探索二：在预测可行的方案里，挑离已做过的配方最远的
    done_v = [vec(d) for d in done] + [vec(d) for d, _ in chosen]
    rest = [ev for ev in pool if ev["design"].key() not in keys]
    rest.sort(key=lambda e: -min((float(np.linalg.norm(vec(e["design"]) - v)) for v in done_v), default=0.0))
    for ev in rest[:n_far]:
        take(ev, "探索：离已做过的远")
    items = list(chosen)
    items += [(base_design(q.get("t_ref", DEFAULT_T_REF)), "现配方对照")]
    rn, saved = _save_round(items, q.get("t_ref", DEFAULT_T_REF))
    xlsx = export_round(rn, q.get("t_ref", DEFAULT_T_REF))
    docx = export_round_docx(rn, q.get("t_ref", DEFAULT_T_REF))
    return {"round": rn, "trials": saved, "path": str(docx), "xlsx": str(xlsx), "docx": str(docx)}
