"""分档最低成本：在给定的料里，按透光三档分别找元/吨最低、且纵横拉断力满足要求的 ABA 方案。

搜索的东西：基底里再生料的比例（A、B 可以不同）、B 层填充比例、B 层 HDPE、A 层占比、厚度。
厚度不进成本（按吨算），只影响拉断力（成正比）和透光（越厚越差），所以每个配方直接取“刚好满足拉断力的最薄厚度”。
两组结果：
- 现有料：只用 LLDPE / 再生回料 / 填充母料 / HDPE（你们现在在用的）；
- 加新料：允许库里其它买得到的料替换或加入（不同来源/品级的料，系数是按类别给的，必须小试验证）。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from . import prices as P
from . import rules as R
from .model import BASE_A_RATIO, BASE_RECYCLED, BASE_RESIN, DEFAULT_T_REF, Ctx, Design, base_design

FILLER_CODE = "FL"
HDPE_CODE = "HD"
CURRENT_CODES = {BASE_RESIN, BASE_RECYCLED, FILLER_CODE, HDPE_CODE}
R_LEVELS = (0.0, 20.0, 33.3, 45.0, 55.0)         # 基底里再生料的比例（%）


@dataclass
class Query:
    md_req: float | None = 1.0          # 纵向拉断力 ≥ 现配方 × md_req；None = 不管
    td_req: float | None = 1.0          # 横向同上
    grade_min: int | None = None        # 透光至少几档（0/1/2）；None = 三档分别给
    t_min: float = 3.0
    t_max: float = 6.0
    t_step: float = 0.5
    t_ref: float = DEFAULT_T_REF        # 现配方对照厚度
    ref_filler: float = 0.0             # 现配方 B 层填充 %
    ref_hdpe: float = 0.0               # 现配方 B 层 HDPE %
    ref_a_ratio: float = BASE_A_RATIO
    a_min: float = 0.30
    a_max: float = 0.50
    filler_max: float = 50.0
    hdpe_max: float = 10.0
    a_recycled_max: float = 33.3        # 表层再生料不超过现配方水平（表面质量）
    b_recycled_max: float = 55.0
    use_new: bool = True
    scenario: str = "current"
    custom_pct: float = 0.0
    top_k: int = 3
    exclude: list[str] = field(default_factory=list)
    optimistic: bool = False            # True = 透光按点估计判档（找“拿不准”的候选用）；默认按八成把握


def _blend(r: float, resin: str, recycled: str) -> dict[str, float]:
    return {resin: 100 - r, recycled: r} if r > 0 else {resin: 100.0}


def _scale(comp: dict[str, float], f: float) -> dict[str, float]:
    return {k: v * f for k, v in comp.items()}


def _merge(*parts: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in parts:
        for k, v in p.items():
            if v > 1e-9:
                out[k] = out.get(k, 0.0) + v
    return {k: round(v, 2) for k, v in out.items()}


ALT_MAX_AGE_DAYS = 30          # 新料的价格超过一个月没更新，不拿来和现有料比（旧价配新价会算出假的“省”）
ALT_MIN_RATIO = 0.8            # PE 新料价格低于同类现有料八成：多半抽错或不是同一种东西，先询价
SAME_FAMILY = {"lldpe": BASE_RESIN, "lldpe_c6": BASE_RESIN, "mlldpe": BASE_RESIN, "ldpe": "LD", "hdpe": HDPE_CODE, "mdpe": HDPE_CODE}


def alt_problem(ctx: Ctx, code: str, eff: dict[str, dict[str, Any]] | None = None) -> str:
    """新料（不是现有四种）不能进方案的原因；能用返回空串。"""
    from datetime import date as _date

    from ..formulation.materials import _as_date, effective_prices

    if code in CURRENT_CODES:
        return ""
    e = (eff if eff is not None else effective_prices()).get(code)
    if not e:
        return "没有价格"
    d = _as_date(e.get("date"))
    if not d or d > _date.today():
        return "价格没有可信的日期"
    age = (_date.today() - d).days
    if age > ALT_MAX_AGE_DAYS:
        return f"价格是 {d} 的（{age} 天前）"
    base = SAME_FAMILY.get(ctx.role.get(code, ""))
    if base and ctx.prices.get(base) and e["price"] < ALT_MIN_RATIO * ctx.prices[base]:
        return f"{e['price']:.0f} 元/吨比同类行情 {ctx.prices[base]:.0f} 低太多，可能抽错"
    return ""


def _usable(ctx: Ctx, exclude: set[str], skipped: dict[str, str] | None = None) -> dict[str, list[str]]:
    """按角色分组的可用料：有价、没被排除、没标“不用”、买得到；新料还要价格新、像样；同一角色里按价格从低到高。"""
    from .. import procurement
    from ..formulation.materials import effective_prices

    eff = effective_prices()
    groups: dict[str, list[str]] = {}
    for code, m in ctx.mats.items():
        if code in exclude or not m.get("active", 1) or str(m.get("use_flag") or "").startswith("否"):
            continue
        if code not in ctx.prices or not procurement.is_buyable(code):
            continue
        why = alt_problem(ctx, code, eff)
        if why:
            if skipped is not None:
                skipped[code] = why
            continue
        groups.setdefault(ctx.role[code], []).append(code)
    for role in groups:
        groups[role].sort(key=lambda c: ctx.prices[c])
    return groups


def _variants(groups: dict[str, list[str]], use_new: bool) -> list[dict[str, Any]]:
    """“换什么料 / 加什么料”的组合：第一个永远是现有料，其余每个只动一处，便于看清每种新料的作用。"""
    out = [{"name": "现有料", "resin": BASE_RESIN, "recycled": BASE_RECYCLED, "filler": FILLER_CODE, "hdpe": HDPE_CODE,
            "addon": None}]
    if not use_new:
        return out
    for code in groups.get("lldpe", []) + groups.get("lldpe_c6", []):
        if code != BASE_RESIN:
            out.append({**out[0], "name": f"主料换 {code}", "resin": code})
    for code in groups.get("recycled_ld", []) + groups.get("recycled_ll", []):
        if code != BASE_RECYCLED:
            out.append({**out[0], "name": f"再生料换 {code}", "recycled": code})
    for code in groups.get("filler", []) + groups.get("clear_filler", []) + groups.get("talc", []):
        if code != FILLER_CODE:
            out.append({**out[0], "name": f"填充换 {code}", "filler": code})
    for code in groups.get("hdpe", []) + groups.get("mdpe", []):
        if code != HDPE_CODE:
            out.append({**out[0], "name": f"芯层挺度料换 {code}", "hdpe": code})
    for role in ("mlldpe", "ldpe", "elastomer", "nucleator"):
        for code in groups.get(role, [])[:2]:
            layers = R.ROLES[role]["layers"]
            cap = R.ROLES[role]["cap"]
            levels = (0.3,) if role == "nucleator" else tuple(x for x in (10.0, 20.0) if x <= cap)
            for where in ("A", "B", "AB"):
                if not set(where) <= set(layers):
                    continue
                for lv in levels:
                    out.append({**out[0], "name": f"{where} 层加 {code} {lv:g}%", "addon": (code, where, lv)})
    return out


def _compose(v: dict[str, Any], r_a: float, r_b: float, f: float, h: float, a: float) -> Design | None:
    """A 层 = 基底（可加料）；B 层 = 基底 × 剩余比例 + 填充 f + HDPE h（加的料替掉一部分基底）。"""
    base_a = _blend(r_a, v["resin"], v["recycled"])
    base_b = _blend(r_b, v["resin"], v["recycled"])
    add = v.get("addon")
    a_layer = dict(base_a)
    if add and "A" in add[1]:
        a_layer = _merge(_scale(base_a, (100 - add[2]) / 100), {add[0]: add[2]})
    b_rest = 100 - f - h
    parts: list[dict[str, float]] = []
    if add and "B" in add[1]:
        if b_rest - add[2] < 10:
            return None
        parts += [_scale(base_b, (b_rest - add[2]) / 100), {add[0]: add[2]}]
    else:
        parts.append(_scale(base_b, b_rest / 100))
    if f:
        parts.append({v["filler"]: f})
    if h:
        parts.append({v["hdpe"]: h})
    return Design(a=a_layer, b=_merge(*parts), a_ratio=round(a, 3), thickness=0.0, tags=[v["name"]])


def _t_needed(md_rel1: float, td_rel1: float, q: Query) -> float | None:
    """同厚度下的拉断力倍数 → 满足要求的最薄厚度（按 t_step 取整），超出 t_max 就不可行。"""
    need = q.t_min
    for rel, req in ((md_rel1, q.md_req), (td_rel1, q.td_req)):
        if req is None:
            continue
        if rel <= 0:
            return None
        need = max(need, q.t_ref * req / rel)
    t = math.ceil(round(need / q.t_step, 6)) * q.t_step
    t = max(q.t_min, t)
    return round(t, 2) if t <= q.t_max + 1e-9 else None


def _signature(item: dict[str, Any]) -> tuple:
    """备选方案要“不一样”：换料思路、填充档（每 10%）、HDPE、再生比例不同才算另一个。"""
    m = item["meta"]
    return (m["variant"], int(m["f"] // 10), m["h"], m["r_b"])


def run(q: Query | dict | None = None) -> dict[str, Any]:
    q = q if isinstance(q, Query) else Query(**(q or {}))
    scen = P.build(q.scenario, q.custom_pct)
    ctx = Ctx(prices=scen["prices"])
    ref = base_design(q.t_ref, filler=q.ref_filler, hdpe=q.ref_hdpe, a_ratio=q.ref_a_ratio)
    ref_eval = ctx.evaluate(ref, ref)
    ref_eval["drivers"] = []
    notes = list(scen["notes"])
    exclude = set(q.exclude or [])
    skipped: dict[str, str] = {}
    groups = _usable(ctx, exclude, skipped)
    missing = [c for c in CURRENT_CODES if c not in ctx.prices]
    if missing:
        notes.append(f"现有料里 {'、'.join(missing)} 没有价格，含它的方案算不出成本。")
    variants = _variants(groups, q.use_new)
    md0, td0, tr0 = ctx.indices(ref)
    a_levels = [round(x / 100, 2) for x in range(int(round(q.a_min * 100)), int(round(q.a_max * 100)) + 1, 5)]
    pairs = [(r, r) for r in R_LEVELS] + [(ra, rb) for ra in (0.0, 20.0, 33.3) for rb in R_LEVELS if rb > ra + 1]
    pairs = [(ra, rb) for ra, rb in pairs if ra <= q.a_recycled_max + 0.1 and rb <= q.b_recycled_max + 0.1]
    f_levels = [float(x) for x in range(0, int(q.filler_max) + 1, 5)]
    h_levels = [x for x in (0.0, 5.0, 10.0, 15.0) if x <= q.hdpe_max + 1e-9]
    results: dict[str, list[dict[str, Any]]] = {"现有料": [], "加新料": []}
    n_eval = 0
    for v in variants:
        if v["filler"] not in ctx.prices:
            f_iter = [0.0]
        else:
            f_iter = f_levels
        h_iter = h_levels if v["hdpe"] in ctx.prices else [0.0]
        for r_a, r_b in pairs:
            if r_b > 0 and v["recycled"] not in ctx.prices:
                continue
            for f in f_iter:
                for h in h_iter:
                    if f + h > 90:
                        continue
                    for a in a_levels:
                        d = _compose(v, r_a, r_b, f, h, a)
                        n_eval += 1
                        if d is None:
                            continue
                        cost = ctx.cost(d)
                        if cost is None:
                            continue
                        md, td, tr = ctx.indices(d)
                        t = _t_needed(md / md0, td / td0, q)
                        if t is None:
                            continue
                        d.thickness = t
                        if ctx.check(d):
                            continue
                        score = tr + ctx.kt * (t - q.t_ref)
                        grade = R.grade_of(score) if q.optimistic else ctx.grade_safe(score, d)   # 默认按“八成把握”承诺档位
                        if q.grade_min is not None and grade < q.grade_min:
                            continue
                        results["现有料" if v["name"] == "现有料" else "加新料"].append(
                            {"design": d, "cost": cost, "grade": grade, "score": score,
                             "meta": {"variant": v["name"], "f": f, "h": h, "r_a": r_a, "r_b": r_b}})
    groups_out: dict[str, dict[int, list[dict[str, Any]]]] = {}
    grades = [q.grade_min] if q.grade_min is not None else [2, 1, 0]
    for gname, items in results.items():
        items.sort(key=lambda x: (x["cost"], x["design"].thickness))
        per: dict[int, list[dict[str, Any]]] = {}
        for g in grades:
            picked, seen = [], set()
            for it in items:
                if it["grade"] < g:
                    continue
                sig = _signature(it)
                if sig in seen:
                    continue
                seen.add(sig)
                ev = ctx.evaluate(it["design"], ref)
                ev["drivers"] = ctx.drivers(it["design"], ref)
                if ref_eval["cost"]:
                    ev["saving"] = round(ref_eval["cost"] - ev["cost"], 1)
                    ev["saving_pct"] = round((ref_eval["cost"] - ev["cost"]) / ref_eval["cost"] * 100, 2)
                picked.append(ev)
                if len(picked) >= q.top_k:
                    break
            per[g] = picked
        groups_out[gname] = per
    if not q.use_new:
        groups_out.pop("加新料", None)
    elif not any(groups_out.get("加新料", {}).values()):
        notes.append("库里除了现有的 4 种料，还没有买得到、有价的新料可用：先在「原料与价格 › 智能体找新料」或对话里让它去找。")
    else:
        # 新料方案要和“只用现有料”的最优比：贵了就直说不划算
        for g, items in groups_out["加新料"].items():
            best = (groups_out.get("现有料", {}).get(g) or [None])[0]
            for ev in items:
                if best and best.get("cost") is not None and ev.get("cost") is not None:
                    ev["vs_current"] = round(ev["cost"] - best["cost"], 1)
            # 只留真比现有料最优省的：同价的替身（如同一个 7042 换个代码）没有意义
            groups_out["加新料"][g] = [ev for ev in items if ev.get("vs_current") is None or ev["vs_current"] < -1]
        better = [ev for items in groups_out["加新料"].values() for ev in items]
        if not better:
            notes.append("按现在的价格和系数，加/换新料都没有比只用现有料更省：新料的价值主要在提性能，降本还得靠 B 层填充和厚度。")
    # 某档按八成把握一个都没有、但点估计有：给出“拿不准”的最省候选，建议试一组确认
    uncertain: dict[int, dict[str, Any]] = {}
    if not q.optimistic and ctx.t_cov is not None:
        for g in grades:
            if groups_out.get("现有料", {}).get(g):
                continue
            alt = run(Query(**{**asdict(q), "optimistic": True, "grade_min": g, "top_k": 1, "use_new": False}))
            ev = (alt["groups"].get("现有料", {}).get(g) or [None])[0]
            if ev:
                uncertain[g] = ev
                notes.append(f"透光≥{R.GRADE_LABEL[g]}：按现有实验数据还没有八成把握做到；点估计最省的是 {ev['text']}，"
                             f"{ev['cost']:.0f} 元/吨（{saving_text(ev.get('saving_pct'))}）——建议试一组确认。")
    if q.use_new and skipped:
        notes.append("这些料没进“加新料”（要用先查价/询价）：" + "；".join(f"{c} {ctx.mats.get(c, {}).get('name', '')}：{w}"
                                                              for c, w in list(skipped.items())[:8])
                     + (f" 等 {len(skipped)} 种" if len(skipped) > 8 else "") + "。")
    notes.append("按吨计价：厚度不改变每吨价格，所以每个方案都取了刚好满足拉断力要求的最薄厚度。"
                 "填充多、厚度大的方案每吨便宜但每平方米用料多——“元/m²(参考)”一栏给出同一张膜的料钱，"
                 "客户按面积买或厚度有规格时请把厚度固定再算。")
    return {"query": asdict(q), "scenario": {k: scen[k] for k in ("name", "label", "ratio", "ref")}, "ref": ref_eval,
            "groups": groups_out, "notes": notes, "n_evaluated": n_eval, "uncertain": uncertain}


# ---------------- 给人看的 ----------------

def saving_text(pct: float | None) -> str:
    if pct is None:
        return "现配方缺价"
    return f"降本 {pct:.1f}%" if pct >= 0 else f"比现配方贵 {-pct:.1f}%"


def _rel(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}×"


def rows(out: dict[str, Any]) -> list[dict[str, Any]]:
    """扁平化成表格行（页面、导出、存库都用它）。"""
    table = []
    for gname, per in out["groups"].items():
        for g, items in per.items():
            for i, ev in enumerate(items, 1):
                table.append({"分组": gname, "透光要求": f"≥{R.GRADE_LABEL[g]}", "名次": i, "配方": ev["text"],
                              "元/吨": ev["cost"], "较现配方": saving_text(ev.get("saving_pct")),
                              "比现有料最优": ev.get("vs_current"), "克重 g/m²": ev["gsm"],
                              "元/m²(参考)": ev.get("cost_m2"),
                              "纵向拉断力": ev["md_rel"], "横向拉断力": ev["td_rel"], "透光": ev["grade_label"],
                              "把握": ev["confidence"], "依据": "；".join(ev.get("drivers") or []), "_ev": ev})
    return table


def to_text(out: dict[str, Any], max_per_grade: int = 2) -> str:
    q = out["query"]
    ref = out["ref"]
    req = []
    for k, label in (("md_req", "纵向"), ("td_req", "横向")):
        if q.get(k) is not None:
            req.append(f"{label}拉断力 ≥ 现配方 × {q[k]:g}")
    lines = [f"价格：{out['scenario']['label']}｜要求：{'，'.join(req) or '拉断力不限'}｜厚度 {q['t_min']:g}~{q['t_max']:g} 丝"
             f"｜现配方对照：{ref['text']}，{ref['cost'] or '缺价'} 元/吨，透光 {ref['grade_label']}"]
    for gname, per in out["groups"].items():
        lines.append(f"## {gname}")
        for g, items in per.items():
            if not items:
                unsure = (out.get("uncertain") or {}).get(g)
                lines.append(f"- 透光 ≥{R.GRADE_LABEL[g]}：" + ("加/换新料不比只用现有料更省" if gname == "加新料" else
                                                                 (f"还没有八成把握；点估计最省 {unsure['text']}，{unsure['cost']:.0f} 元/吨，建议试一组确认"
                                                                  if unsure else "在约束内找不到方案")))
                continue
            for i, ev in enumerate(items[:max_per_grade], 1):
                lines.append(f"- 透光 ≥{R.GRADE_LABEL[g]} #{i}：{ev['text']}｜{ev['cost']:.0f} 元/吨"
                             f"（{saving_text(ev.get('saving_pct'))}）｜纵 {_rel(ev['md_rel'])} 横 {_rel(ev['td_rel'])}"
                             f"｜预测透光 {ev['grade_label']}｜克重 {ev['gsm']:.0f}（每平方米料钱 {ev.get('cost_m2') or 0:.3f} 元）"
                             f"｜把握 {ev['confidence']}"
                             + (f"｜比现有料最优 {ev['vs_current']:+.0f} 元/吨" if ev.get("vs_current") is not None else ""))
                for dline in (ev.get("drivers") or [])[:3]:
                    lines.append(f"    · {dline}")
    lines += [f"· {n}" for n in out["notes"]]
    return "\n".join(lines)
