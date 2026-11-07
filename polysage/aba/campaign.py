"""跑一整轮：更新行情 → 清掉明显抽错的价格 → 找新料 → 分档寻优（当前价 + 期货价）→ 出 N 组试验方案 → Word 报告。

产出两份 Word（知识库 06_实验数据）：
- 《第 N 轮试验方案》：配方表、投料单、测试约定、数据记录表（填好直接上传回来）；
- 《第 N 轮运行报告》：价格、现配方成本、每档最省方案、价格变化下的最省方案、新材料、这一轮怎么安排、下一步。
报告内容同时存成 round_N_report.json，网页「实验与校准 › 各轮结果」直接显示。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .. import db
from ..config import KB
from . import experiments as E
from . import optimize as O
from . import prices as P
from . import rules as R
from .model import DEFAULT_T_REF, Ctx, layer_text

# 价格常识区间（元/吨）：网上抽到的价格不在区间里多半是抽错了（如把 12100 抽成 121000）
CAT_RANGE = {"主体树脂": (5000, 30000), "再生料": (1500, 15000), "再生PE": (1500, 15000), "弹性体": (8000, 45000),
             "韧性/热封改性": (8000, 45000), "填充": (1000, 20000), "填充母料": (1000, 20000)}


def clean_prices() -> list[str]:
    """删掉不在常识区间里的报价（只删网查 / 估计来的，甲方给的实价不动）。返回删了哪些。"""
    rows = db.q("SELECT p.id, p.material_code, p.price, p.price_type, p.source, m.category, m.name FROM prices p "
                "JOIN materials m ON m.code = p.material_code")
    removed = []
    for r in rows:
        rng = CAT_RANGE.get(r["category"] or "")
        if not rng or r["price_type"] == "actual":
            continue
        lo, hi = rng
        if not (lo <= float(r["price"]) <= hi):
            db.execute("DELETE FROM prices WHERE id=?", (r["id"],))
            removed.append(f"{r['material_code']} {r['name']}：{r['price']:.0f} 元/吨（{r['category']}常识区间 {lo}~{hi}）")
    return list(dict.fromkeys(removed))


def _flat(v: Any) -> str:
    if isinstance(v, dict):
        return "，".join(f"{k} {x}" for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return "，".join(map(str, v))
    return str(v)


def _pct(ref: float | None, cost: float | None) -> str:
    if not ref or cost is None:
        return ""
    p = (ref - cost) / ref * 100
    return "持平" if abs(p) < 0.05 else (f"省 {p:.1f}%" if p > 0 else f"贵 {-p:.1f}%")


def _m2s(v: float | None) -> str:
    return f"{v:.3f}" if v else ""


def _scenarios() -> list[dict[str, Any]]:
    out = []
    for name in ("current", "live", "fut1", "fut2"):
        try:
            s = P.build(name)
        except Exception as e:  # noqa: BLE001
            s = {"name": name, "label": P.SCENARIOS[name], "prices": {}, "ratio": 1.0, "notes": [f"取不到：{e}"], "ref": {}}
        if s["name"] != name:                      # 期货取不到时会退回当前价，别重复列
            continue
        out.append(s)
    return out


class Blocks(list):
    """报告内容块：同一份既排成 Word（report_docx），也存成 JSON 在网页「实验与校准 › 各轮结果」显示。"""

    def h(self, text: str, level: int = 1) -> None:
        self.append({"t": "h", "text": text, "level": level})

    def p(self, text: str) -> None:
        self.append({"t": "p", "text": text})

    def ul(self, items: Any, numbered: bool = False) -> None:
        self.append({"t": "ul", "items": [str(x) for x in items], "numbered": numbered})

    def note(self, text: str) -> None:
        self.append({"t": "note", "text": text})

    def table(self, rows: Any, widths_cm: list[float] | None = None, font_size: float = 9.0) -> None:
        self.append({"t": "table", "rows": [{k: _plain(v) for k, v in r.items()} for r in rows],
                     "widths": widths_cm, "font": font_size})


def _plain(v: Any) -> Any:
    """numpy 数字等转成 JSON 能存的。"""
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


FEEDBACK_CODES = ("LL", "LLC", "LD", "HD", "R1", "FL")


def _feedback(round_no: int, ref: dict[str, Any]) -> dict[str, Any]:
    """第 2 轮起报告里多一节：价格比上一轮变了多少、已回填的实验教会了模型什么、实测验证过的最省配方。"""
    out: dict[str, Any] = {}
    blocks = Blocks()
    ctx = Ctx()
    if round_no > 1:
        before = E.round_prices(round_no - 1)
        rows = []
        for c in [c for c in FEEDBACK_CODES if c in before and ctx.prices.get(c)]:
            old, new = before[c], float(ctx.prices[c])
            rows.append({"代码": c, "名称": ctx.mats.get(c, {}).get("name", ""), f"第 {round_no - 1} 轮(元/吨)": round(old),
                         "现在(元/吨)": round(new), "变化": f"{(new - old) / old * 100:+.1f}%" if old else ""})
        if rows:
            moved = [r for r in rows if r["变化"] and abs(float(r["变化"].rstrip("%"))) >= 0.05]
            out["price_line"] = (f"**价格（和第 {round_no - 1} 轮比）**：" + "；".join(f"{r['代码']} {r[f'第 {round_no - 1} 轮(元/吨)']}→{r['现在(元/吨)']}"
                                                                          f"（{r['变化']}）" for r in moved) + "。"
                                 if moved else f"**价格（和第 {round_no - 1} 轮比）**：主要原料价格没变。")
            blocks.h(f"价格和第 {round_no - 1} 轮比", level=2)
            blocks.table(rows, widths_cm=[1.3, 5.0, 2.6, 2.2, 1.6], font_size=8.5)
    meas = E.measured()
    cal = R.calibration()
    if meas:
        by_round: dict[int, int] = {}
        for r in meas:
            by_round[r["round"]] = by_round.get(r["round"], 0) + 1
        best = E.verified_best()
        head = f"已回填 {len(meas)} 组（" + "，".join(f"第 {k} 轮 {v} 组" for k, v in sorted(by_round.items())) + "）"
        if best:
            parts = [f"透光≥{R.GRADE_LABEL[g]} {b['code']} {b['cost']} 元/吨（{_pct(ref.get('cost'), b['cost'])}）"
                     for g, b in sorted(best.items(), reverse=True)]
            out["measured_line"] = f"**实验反馈**：{head}；实测验证过、拉断力不低于现配方的最省：" + "；".join(parts) + "。"
        else:
            out["measured_line"] = f"**实验反馈**：{head}；还没有实测拉断力不低于现配方、又有透光档的组。"
        blocks.h("已回填的实验：模型学到了什么", level=2)
        if cal.get("n"):
            out["cal"] = cal
            blocks.p(f"用 {cal['n']} 组实测校准（{str(cal.get('date', ''))[:16].replace('T', ' ')}）：现配方 {DEFAULT_T_REF:g} 丝 "
                     f"纵向约 {cal.get('md_N_ref')} N、横向约 {cal.get('td_N_ref')} N；拉断力拟合误差 纵 {cal.get('md_rmse_pct')}% / "
                     f"横 {cal.get('td_rmse_pct')}%；透光档命中 {cal.get('grade_hit')}%。这一轮的最省方案都按校准后的系数挑。")
            prior = R.load_table(include_cal=False)
            coef = [{"料": prior.get(role, {}).get("label", role), "用到的组数": v.get("n"),
                     "纵向 先验→校准": f"{prior.get(role, {}).get('md', 0):+.3f} → {v.get('md', prior.get(role, {}).get('md', 0)):+.3f}",
                     "横向 先验→校准": f"{prior.get(role, {}).get('td', 0):+.3f} → {v.get('td', prior.get(role, {}).get('td', 0)):+.3f}",
                     "透光 先验→校准": f"{prior.get(role, {}).get('trans', 0):+.2f} → {v.get('trans', prior.get(role, {}).get('trans', 0)):+.2f}"}
                    for role, v in (cal.get("roles") or {}).items()]
            if coef:
                blocks.table(coef, widths_cm=[3.4, 1.8, 3.2, 3.2, 3.2], font_size=8.5)
                blocks.note("系数是每 10 个百分点（占整膜）对强度（对数）和透光分的影响；数据少时往先验收缩，数据多了才明显改。")
        else:
            blocks.p("回填还不到 4 组（要含现配方对照），还没校准；这一轮仍按定性系数。")
        if best:
            blocks.p("**实测验证过的最省配方**（纵/横拉断力实测不低于现配方，透光实测达到该档，按今天的价格）：")
            blocks.table([{"透光要求": f"≥{R.GRADE_LABEL[g]}", "编号": b["code"], "配方": b["design"].text(), "元/吨": b["cost"],
                           "较现配方": _pct(ref.get("cost"), b["cost"]), "实测纵/横 N": f"{b['md_n']:g} / {b['td_n']:g}",
                           "现配方纵/横 N": f"{b['md_ref']:g} / {b['td_ref']:g}"} for g, b in sorted(best.items(), reverse=True)],
                         widths_cm=[1.5, 1.4, 7.0, 1.4, 1.5, 2.0, 2.0], font_size=8)
        cmp_ = E.compare()
        if not cmp_.empty:
            rows = [{k: (None if isinstance(v, float) and v != v else v) for k, v in r.items()} for r in cmp_.to_dict("records")]
            blocks.p(f"**预测 vs 实测**（用现在的系数重新预测，共 {len(rows)} 组" + ("，列前 30 组" if len(rows) > 30 else "") + "）：")
            blocks.table(rows[:30], widths_cm=[1.4, 6.0, 1.4, 1.4, 1.4, 1.4, 1.3, 1.3], font_size=7.5)
    if blocks:
        out["title"] = "上一轮反馈：价格变化与实测" if round_no > 1 else "实测反馈"
        out["blocks"] = list(blocks)
    return out


def report_blocks(round_no: int, *, market: dict[str, Any] | None = None, discover: dict[str, Any] | None = None,
                  cleaned: list[str] | None = None, t_ref: float = DEFAULT_T_REF) -> dict[str, Any]:
    """按当前价和期货价重算各档最省方案，排成报告内容块（Word 和网页共用）。"""
    from ..formulation.materials import list_materials

    trials = E.trials(round_no)
    meta = E.round_meta(round_no)
    market = market if market is not None else meta.get("market")          # 这一轮跑的时候记下的行情 / 新料结果
    discover = discover if discover is not None else meta.get("discover")
    cleaned = cleaned if cleaned is not None else meta.get("cleaned")
    scens = _scenarios()
    runs = {}
    for s in scens:
        runs[s["name"]] = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True, "scenario": s["name"], "t_ref": t_ref, "top_k": 3})
    cur = runs["current"]
    ref = cur["ref"]
    fixed = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": False, "t_ref": t_ref, "t_min": t_ref, "t_max": t_ref, "top_k": 3})
    fixed95 = O.run({"md_req": 0.95, "td_req": 0.95, "use_new": False, "t_ref": t_ref, "t_min": t_ref, "t_max": t_ref, "top_k": 1})
    from ..daily_quotes import short_source

    def per_m2(ev: dict[str, Any]) -> str:
        if ev.get("cost_m2") and ref.get("cost_m2"):
            return f"，每平方米 {ev['cost_m2']:.3f} 元（{_pct(ref['cost_m2'], ev['cost_m2'])}）"
        return ""
    B = Blocks()
    nums = iter("一二三四五六七八九十")

    def H(title: str) -> None:                      # 节号自动排：有回填反馈时多一节
        B.h(f"{next(nums)}、{title}", level=1)

    fb = _feedback(round_no, ref)

    # 一、结论
    H("结论（先看这里）")
    ll = cur["scenario"].get("ref") or {}
    concl = []
    if ll.get("LL"):
        src = short_source((P.price_row("LL") or {}).get("source") or ll.get("LL_source", ""))
        by_ratio: dict[float, list[str]] = {}
        for s in scens:
            if s["name"] != "current":
                by_ratio.setdefault(round(s["ratio"], 3), []).append(s["label"])
        concl.append(f"**价格**：LLDPE 价格卡 {ll['LL']:.0f} 元/吨（{ll.get('LL_date')}，{src[:90]}）。"
                     + "".join(f" {' / '.join(labels)}：LLDPE ≈ {ll['LL'] * r:.0f}（×{r:.3f}）。" for r, labels in by_ratio.items()))
    if fb.get("price_line"):
        concl.append(fb["price_line"])
    if fb.get("measured_line"):
        concl.append(fb["measured_line"])
    concl.append(f"**现配方**：{ref['text']}，当前价 **{ref['cost']:.0f} 元/吨**，透光{ref['grade_label']}。" if ref.get("cost")
                 else f"**现配方**：{ref['text']}（缺价）。")
    best_line = []
    for g in (2, 1, 0):
        items = cur["groups"].get("现有料", {}).get(g) or []
        unsure = (cur.get("uncertain") or {}).get(g)
        if not items and unsure:
            best_line.append(f"透光≥{R.GRADE_LABEL[g]}：还没有八成把握（点估计 {unsure['cost']:.0f} 元/吨，建议试一组确认）")
        if items:
            ev = items[0]
            m2 = ""
            if ev.get("cost_m2") and ref.get("cost_m2"):
                m2 = f"，每平方米 {ev['cost_m2']:.3f} 元（{_pct(ref['cost_m2'], ev['cost_m2'])}）"
            best_line.append(f"透光≥{R.GRADE_LABEL[g]}：{ev['cost']:.0f} 元/吨（{_pct(ref.get('cost'), ev['cost'])}，"
                             f"{ev['design'].thickness:g} 丝{m2}）")
    if best_line:
        concl.append("**每档最省（当前价，纵横拉断力不低于现配方，只用现有四种料）**：" + "；".join(best_line) + "。"
                     f"现配方每平方米 {ref.get('cost_m2') or 0:.3f} 元。")
        concl.append("**注意按吨和按面积不一样**：多填充、加厚的方案每吨便宜，但膜更重、每平方米用料更多；"
                     "客户按重量买看元/吨，按面积或卷长买要看元/m²。")
    fx_line, prev_key = [], None
    for g in (2, 1, 0):
        ev = (fixed["groups"].get("现有料", {}).get(g) or [None])[0]
        if ev and ev["design"].key() == prev_key:
            fx_line[-1] = fx_line[-1].replace("：", f"（{R.GRADE_LABEL[g]}同）：", 1)
        elif ev:
            fx_line.append(f"透光≥{R.GRADE_LABEL[g]}：{ev['cost']:.0f} 元/吨（{_pct(ref.get('cost'), ev['cost'])}{per_m2(ev)}）")
        prev_key = ev["design"].key() if ev else None
    if fx_line:
        ev95 = (fixed95["groups"].get("现有料", {}).get(1) or [None])[0]
        concl.append(f"**厚度不变（{t_ref:g} 丝）时每档最省**：" + "；".join(fx_line) + "。"
                     + (f"拉断力放宽到现配方的 95%，透光≥好 能到 {ev95['cost']:.0f} 元/吨（{_pct(ref.get('cost'), ev95['cost'])}）。"
                        if ev95 else ""))
    newer = [ev for items in (cur["groups"].get("加新料") or {}).values() for ev in items]
    concl.append(f"**新料**：有 {len(newer)} 个方案比只用现有料更省（见第六节），要先确认料源和外观。" if newer
                 else "**新料**：按现在的价格和系数，加/换新料都不比只用现有料更省；降本主要靠 B 层填充和厚度。")
    counts = dict(meta.get("counts") or {})
    if not counts:                                   # 下一轮没记 meta：按目的的前缀数
        for tr_ in trials:
            kind = tr_["purpose"].split("：")[0]
            counts[kind] = counts.get(kind, 0) + 1
    concl.append(f"**本轮试验 {len(trials)} 组**：" + "，".join(f"{k} {v} 组" for k, v in counts.items())
                 + f"。配方表、投料单和数据记录表见《ABA第{round_no}轮试验方案》Word。")
    if fb.get("cal"):
        c = fb["cal"]
        concl.append(f"**预测已用 {c.get('n')} 组实测校准**（拉断力拟合误差 纵 {c.get('md_rmse_pct')}% / 横 {c.get('td_rmse_pct')}%，"
                     f"透光档命中 {c.get('grade_hit')}%）；回填越多越准，把握标在每个方案后面。")
    else:
        concl.append("**现在的预测是定性的**（按材料类别的经验系数），把握标在每个方案后面；做完这一轮、回填实测，"
                     "系统会用数据校准系数（机器学习），再给出更准的下一轮。")
    B.ul(concl)

    if fb.get("blocks"):
        H(fb["title"])
        B.extend(fb["blocks"])

    # 二、价格
    H("价格")
    if market:
        label = {"forecast": "价格预判（现价 → 1 个月）", "web": "网查同类报价", "price_card": "写进价格卡",
                 "materials": "回料/填充报价", "web_error": "网查出错", "forecast_error": "预判出错", "materials_error": "回料网查出错"}
        B.p("**这次更新行情**：" + "；".join(f"{label.get(k, k)}：{_flat(v)}" for k, v in market.items()
                                          if k != "at" and v)[:900])
    ctx = Ctx(prices=scens[0]["prices"])
    used = sorted({k for t in trials for k in t["design"].whole()} | set(ref["design"].whole()))
    rows = []
    for c in used:
        pr = P.price_row(c) or {}
        rows.append({"代码": c, "名称": ctx.mats.get(c, {}).get("name", ""), "当前价(元/吨)": ctx.prices.get(c),
                     "报价日期": pr.get("price_date") or "", "类型": ("网查行情（日报过期）" if str(pr.get("why", "")).startswith("网查价") else
                             {"actual": "实价", "estimate": "估计/网查"}.get(pr.get("price_type"), "")),
                     "来源": short_source(pr.get("source") or "")[:70]})
    B.table(rows, widths_cm=[1.3, 4.2, 2.0, 2.0, 1.8, 6.2], font_size=8.5)
    common = [n for n in scens[0]["notes"]]                    # 每个场景都有的那句（LLDPE 按什么价）只说一次
    for n in common:
        B.note(n)
    B.table([{"价格场景": s["label"], "LLDPE(元/吨)": s["prices"].get("LL"), "相对价格卡": f"×{s['ratio']:.3f}",
              "说明": "；".join(n.rstrip("。") for n in s["notes"] if n not in common)[:120]} for s in scens],
            widths_cm=[3.6, 2.4, 2.0, 9.5], font_size=8.5)
    B.note("国内只有 LLDPE 有期货（大商所 L）：新料 PE 按 1.0 联动，再生料按 0.6 跟随，填充料和助剂不变。")
    if cleaned:
        B.note("清掉的明显抽错的网查价：" + "；".join(cleaned))

    # 三、现配方在不同价格下
    H("现配方在不同价格下")
    B.table([{"价格场景": s["label"], "元/吨": runs[s["name"]]["ref"].get("cost"), "克重 g/m²": runs[s["name"]]["ref"].get("gsm"),
              "元/m²(参考)": _m2s(runs[s["name"]]["ref"].get("cost_m2"))} for s in scens], font_size=9)

    # 四、分档最低成本（当前价）
    H("分档最低成本（当前价，纵横拉断力不低于现配方）")
    for gname in ("现有料", "厚度不变", "加新料"):
        per = (fixed if gname == "厚度不变" else cur)["groups"].get("现有料" if gname == "厚度不变" else gname) or {}
        lines, shown = [], set()
        for g in (2, 1, 0):
            items = [ev for ev in (per.get(g) or []) if not (gname == "厚度不变" and ev["design"].key() in shown)]
            shown |= {ev["design"].key() for ev in items}
            for i, ev in enumerate(items, 1):
                d = ev["design"]
                lines.append({"透光要求": f"≥{R.GRADE_LABEL[g]}", "#": i, "A 层": f"{d.a_ratio:.0%}：{layer_text(d.a)}",
                              "B 层": f"{1 - d.a_ratio:.0%}：{layer_text(d.b)}", "厚度": d.thickness, "元/吨": ev["cost"],
                              "较现配方": _pct(ref.get("cost"), ev["cost"]), "纵/横": f"{ev['md_rel']:.2f}/{ev['td_rel']:.2f}",
                              "透光": ev["grade_label"], "元/m²": _m2s(ev.get("cost_m2")), "把握": ev["confidence"]})
        B.h({"现有料": "只用现有四种料（LLDPE / 再生回料 / 填充母料 / HDPE），厚度可调",
             "厚度不变": f"只用现有四种料，厚度不变（{t_ref:g} 丝）：客户按面积买、厚度有规格时看这张",
             "加新料": "加 / 换新料（只列比现有料最优更省的）"}[gname], level=2)
        if lines:
            B.table(lines, widths_cm=[1.5, 0.6, 3.4, 4.2, 1.0, 1.3, 1.5, 1.6, 1.0, 1.2, 1.0], font_size=8)
            if gname == "厚度不变" and not any(ln["透光要求"] == "≥一般" for ln in lines):
                B.note("厚度不变时，“一般”档没有比“好”档更省的方案（再多加填充，拉断力就不够了）。")
        else:
            B.p("没有（在约束内找不到，或不比只用现有料省）。")
        if gname == "现有料":
            for g, ev in (cur.get("uncertain") or {}).items():
                B.p(f"**透光≥{R.GRADE_LABEL[g]}：按现有实验数据还没有八成把握**。点估计最省的是 {ev['text']}，"
                    f"{ev['cost']:.0f} 元/吨（{_pct(ref.get('cost'), ev['cost'])}）——建议试一组确认。")
    tops = [(g, (cur["groups"].get("现有料", {}).get(g) or [None])[0]) for g in (2, 1, 0)]
    for g, ev in tops:
        if ev and ev.get("drivers"):
            B.p(f"**透光≥{R.GRADE_LABEL[g]} 最省方案为什么这样配**：")
            B.ul(ev["drivers"][:4])

    # 五、价格变化下
    H("价格变化下的最省方案")
    lines = []
    for s in scens:
        o = runs[s["name"]]
        for g in (2, 1, 0):
            ev = (o["groups"].get("现有料", {}).get(g) or [None])[0]
            if ev:
                lines.append({"价格场景": s["label"], "透光": f"≥{R.GRADE_LABEL[g]}", "最省方案": ev["text"], "元/吨": ev["cost"],
                              "现配方元/吨": o["ref"].get("cost"), "较现配方": _pct(o["ref"].get("cost"), ev["cost"])})
    B.table(lines, widths_cm=[3.0, 1.3, 8.0, 1.5, 1.7, 1.5], font_size=8)
    B.note("LLDPE 涨价时，填充母料（不跟涨）相对更划算，最省方案会往多填充、稍厚的方向走；跌价时反过来。")

    # 六、新材料
    H("新材料（智能体网上找到、买得到的）")
    if discover:
        for k in ("added", "better", "notes"):
            if discover.get(k):
                B.p({"added": "**这次新入库**：", "better": "**比现有料更省的**：", "notes": "**说明**："}[k]
                    + _flat(discover[k])[:900])
    found = [m for m in list_materials(active_only=False) if (m.get("origin") or "").startswith("智能体")]
    in_best = {k for ev in newer for k in ev["design"].whole()}
    skipped = cur.get("skipped") or {}

    def verdict(code: str) -> str:
        if code in in_best:
            return "进入了更省的方案"
        flag = str((ctx.mats.get(code) or {}).get("use_flag") or "")
        if flag.startswith("否"):
            return "标了不用" + flag[1:]
        if code not in ctx.prices:
            return "缺价"
        w = skipped.get(code)
        if not w:
            return "不比现有料省"
        if w.startswith("线索"):
            m_ = re.search(r"20\d{2}-\d{2}-\d{2}", w)
            old = bool(m_) and (date.today() - date.fromisoformat(m_.group(0))).days > 30
            return "比现有料便宜，询价后可进方案" + (f"（网查价是 {m_.group(0)} 的，先核实）" if old else "")
        return w

    if found:
        B.table([{"代码": m["code"], "名称": m["name"], "类别": m.get("category"),
                  "价格(元/吨)": ctx.prices.get(m["code"]), "发现于": (m.get("discovered_at") or "")[:10],
                  "结论": verdict(m["code"])}
                 for m in found], widths_cm=[1.2, 5.2, 2.0, 1.8, 1.8, 5.0], font_size=8.5)
    else:
        B.p("库里还没有智能体找到的新料。")

    # 七、本轮试验
    H(f"第 {round_no} 轮试验怎么安排")
    B.table([{"类别": k, "组数": v} for k, v in counts.items()], widths_cm=[4, 2], font_size=9)
    pool = sorted((t for t in trials if t["purpose"].startswith(("降本", "利用"))), key=lambda t: t["predicted"].get("cost") or 1e9)
    cheapest = [t for t in pool if "厚度不变" in t["purpose"]]
    for g in (2, 1, 0):                                   # 每档最省的一组（靠加厚的）
        t_ = next((t for t in pool if t["predicted"].get("grade") == g and t not in cheapest), None)
        if t_:
            cheapest.append(t_)
    cheapest = cheapest[:6]
    if cheapest:
        B.p("**预测最省的几组（优先做）**：")
        B.ul([f"{t['code']}：{t['design'].text()}，预计 {t['predicted'].get('cost'):.0f} 元/吨，"
              f"透光{t['predicted'].get('grade_label')}" + ("（厚度不变）" if "厚度不变" in t["purpose"] else "")
              for t in cheapest])

    # 八、下一步
    H("做完以后")
    B.ul([
        f"按《ABA第{round_no}轮试验方案》的投料单打样，按测试约定测纵向 / 横向拉断力（N）、实测厚度，透光按标样判 0/1/2 档。",
        "把方案 Word 最后的数据记录表填好，在网页「实验与校准 › 回填实测」直接上传这个 Word（不用改格式；没做的组空着就行）。"
        "上传后系统自动校准：岭回归（拉断力）和有序回归（透光档）把系数往实测拉，现配方对照组给出拉断力的绝对值（N）。",
        f"到「实验与校准 › 跑一轮」填这一轮要几组（最多 {E.MAX_ROUND} 组）出下一轮：用校准后的模型和当天的价格挑最省的，"
        "加上透光拿不准的几组和补没测过区域的建模组；报告里会写上一轮学到了什么、价格变了多少。",
        "价格变了随时可以问配方助手“按今天的价格/期货价重新算”，或在「各轮结果」点“按今天的价格重算报告”。",
    ], numbered=True)
    B.h("说明与局限", level=2)
    B.ul([n for n in dict.fromkeys(cur["notes"]) if n not in common][:6] + [
        "纵向/横向拉断力是相对现配方（同配方、对照厚度）的倍数；透光三档由定性透光分预测，都需要实验验证。"])
    return {"round": round_no, "title": f"ABA 三层膜降本 · 第 {round_no} 轮运行报告",
            "subtitle": f"对照：现配方 {t_ref:g} 丝　·　{len(trials)} 组试验", "at": datetime.now().isoformat(timespec="minutes"),
            "blocks": list(B)}


def write_docx(rep: dict[str, Any], path: Path) -> Path:
    from .. import wordout as W

    doc = W.new_doc(rep["title"], rep.get("subtitle", ""))
    for b in rep["blocks"]:
        kind = b.get("t")
        if kind == "h":
            doc.add_heading(b["text"], level=b.get("level", 1))
        elif kind == "p":
            W.para(doc, b["text"])
        elif kind == "ul":
            W.bullets(doc, b["items"], numbered=b.get("numbered", False))
        elif kind == "note":
            W.note(doc, b["text"])
        elif kind == "table" and b.get("rows"):
            W.add_table(doc, b["rows"], widths_cm=b.get("widths"), font_size=b.get("font", 9.0))
    return W.save(doc, path)


def report_json(round_no: int) -> Path:
    return KB["experiment"] / f"round_{round_no}_report.json"


def report_docx(round_no: int, *, market: dict[str, Any] | None = None, discover: dict[str, Any] | None = None,
                cleaned: list[str] | None = None, t_ref: float = DEFAULT_T_REF) -> Path:
    """生成《运行报告》Word，同时把内容存成 JSON（网页直接显示，不用再算）。"""
    rep = report_blocks(round_no, market=market, discover=discover, cleaned=cleaned, t_ref=t_ref)
    path = write_docx(rep, KB["experiment"] / f"ABA第{round_no}轮运行报告_{date.today().isoformat()}.docx")
    rep["docx"] = str(path)
    report_json(round_no).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_report(round_no: int) -> dict[str, Any] | None:
    """上次生成的运行报告内容（没有或读不了就 None）。"""
    p = report_json(round_no)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def round_files(round_no: int) -> dict[str, str]:
    """这一轮最新的 Word / Excel（页面下载用）。"""
    out = {}
    for key, pat in (("plan_docx", f"ABA第{round_no}轮试验方案_*.docx"), ("plan_xlsx", f"ABA第{round_no}轮试验方案_*.xlsx"),
                     ("report_docx", f"ABA第{round_no}轮运行报告_*.docx")):
        files = sorted(KB["experiment"].glob(pat), key=lambda f: f.stat().st_mtime)
        if files:
            out[key] = str(files[-1])
    return out


def run(n_total: int = 50, *, refresh_market: bool = True, find_new: bool = True, scenario: str = "current",
        t_ref: float = DEFAULT_T_REF, replace: bool = False) -> dict[str, Any]:
    """一键跑一整轮（后台任务用）。每一步失败都不影响后面，原因写进报告。"""
    from ..agents import tools_ops as OPS

    t0 = time.time()
    steps: dict[str, Any] = {}
    market = discover = None
    if refresh_market:
        try:
            market = OPS._market_job()
            steps["行情"] = "已更新"
        except Exception as e:  # noqa: BLE001
            steps["行情"] = f"失败：{e}"
    cleaned = clean_prices()
    if find_new:
        try:
            discover = OPS._new_materials_job()
            steps["新料"] = f"新入库 {len(discover.get('added') or [])} 种"
        except Exception as e:  # noqa: BLE001
            steps["新料"] = f"失败：{e}"
    plan = E.plan_round(n_total, t_ref=t_ref, scenario=scenario, replace=replace)
    cal = plan["meta"].get("calibration") or {}
    steps["校准"] = (f"用 {cal.get('n')} 组实测" + ("重新校准" if cal.get("fresh") else "（沿用上次的校准）") if cal.get("ok")
                   else "还没有足够的回填数据，按定性系数")
    if plan["meta"].get("replaced"):
        steps["替换"] = f"作废了第 {plan['round']} 轮原来的 {plan['meta']['replaced']} 组（还没回填）"
    from .. import kb

    meta_path = KB["experiment"] / f"round_{plan['round']}_meta.yaml"
    kb.write_yaml(meta_path, {**E.round_meta(plan["round"]), "market": market, "discover": discover, "cleaned": cleaned,
                              "steps": steps})
    report = report_docx(plan["round"], market=market, discover=discover, cleaned=cleaned, t_ref=t_ref)
    minutes = round((time.time() - t0) / 60, 1)
    kb.write_yaml(meta_path, {**E.round_meta(plan["round"]), "minutes": minutes,
                              "finished_at": datetime.now().isoformat(timespec="minutes")})
    return {"round": plan["round"], "n": len(plan["trials"]), "plan_docx": plan["docx"], "plan_xlsx": plan["xlsx"],
            "report_docx": str(report), "counts": plan["meta"]["counts"], "cleaned": cleaned, "steps": steps,
            "minutes": minutes}


def latest_files() -> dict[str, str]:
    """最近一轮的 Word（页面下载用）。"""
    out = {}
    for kind in ("试验方案", "运行报告"):
        files = sorted(KB["experiment"].glob(f"ABA第*轮{kind}_*.docx"), key=lambda p: p.stat().st_mtime)
        if files:
            out[kind] = str(files[-1])
    return out

