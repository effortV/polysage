"""采购方案：给定配方 → 每种料买谁家、什么价、怎么联系、按吨产品的用量与小计 → 采购清单（xlsx）。

价格来源优先级（每种料取最好的一条）：
1. 实价（已登记的正式报价 / 询价核实）
2. 贸易商日报当日最低到厂价（主料 LL/LLC/LD/HD/mLL）
3. 网查最低到厂价（辅料与再生料，近一个月；主料近一周）
4. 价格卡估计价（没有任何报价时，标“待询价”）
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pandas as pd

from . import daily_quotes as DQ
from . import db
from .config import KB
from .formulation import cost as COST
from .formulation import materials as MAT

RFQ_PATH = KB["price"] / "采购清单.xlsx"
TIER_LABEL = {"actual": "实价（已登记）", "daily": "贸易商日报", "web": "网查", "estimate": "估计价（待询价）", "none": "无价格"}


def _row_source(r: dict[str, Any], tier: str) -> dict[str, Any]:
    return {
        "tier": tier, "price": float(r["landed_price"] if r.get("landed_price") is not None else r["price"]),
        "quoted": float(r["price"]), "supplier": r.get("supplier") or r.get("trader") or "",
        "grade": r.get("grade") or "", "region": r.get("warehouse") or r.get("region") or "",
        "delivery": r.get("delivery") or "", "moq": r.get("moq") or "", "contact": r.get("contact") or "",
        "date": r.get("quote_date") or "", "url": r.get("source_url") or "", "basis": r.get("basis") or "",
    }


def best_source(code: str) -> dict[str, Any]:
    """一种料当前最好的采购来源。"""
    m = MAT.get_material(code) or {"code": code, "name": code}
    prices = MAT.current_prices().get(code, {})
    # 1) 实价
    if prices.get("actual") is not None:
        last = db.q1("SELECT * FROM prices WHERE material_code=? AND price_type='actual' ORDER BY id DESC LIMIT 1", (code,))
        return {"code": code, "name": m.get("name", code), "tier": "actual", "price": float(prices["actual"]),
                "quoted": float(prices["actual"]), "supplier": (last or {}).get("supplier") or "", "grade": m.get("grade") or "",
                "region": "", "delivery": "", "moq": (last or {}).get("moq") or "", "contact": "",
                "date": (last or {}).get("price_date") or "", "url": (last or {}).get("url") or "", "basis": "到厂含税",
                "note": (last or {}).get("source") or ""}
    # 2) 日报（主料）
    fam = next((f for f, codes in DQ.FAMILY_CODES.items() if code in codes), None)
    if fam:
        rows = [r for r in DQ.daily_rows() if r.get("family") == fam]
        if rows:
            return {"code": code, "name": m.get("name", code), **_row_source(rows[0], "daily")}
    # 3) 网查
    rows = DQ.material_rows(code, top=1)
    if not rows and fam:
        rows = DQ.web_rows(fam, top=1)
    if rows:
        return {"code": code, "name": m.get("name", code), **_row_source(rows[0], "web")}
    # 4) 估计价
    if prices.get("estimate") is not None:
        return {"code": code, "name": m.get("name", code), "tier": "estimate", "price": float(prices["estimate"]), "quoted": float(prices["estimate"]),
                "supplier": "", "grade": m.get("grade") or "", "region": "", "delivery": "", "moq": "", "contact": "", "date": "", "url": "", "basis": ""}
    return {"code": code, "name": m.get("name", code), "tier": "none", "price": None, "quoted": None, "supplier": "", "grade": m.get("grade") or "",
            "region": "", "delivery": "", "moq": "", "contact": "", "date": "", "url": "", "basis": ""}


def alternatives(code: str, top: int = 3) -> list[dict[str, Any]]:
    """备选来源（次便宜的几家），日报与网查合并。"""
    out: list[dict[str, Any]] = []
    fam = next((f for f, codes in DQ.FAMILY_CODES.items() if code in codes), None)
    if fam:
        out += [_row_source(r, "daily") for r in DQ.daily_rows() if r.get("family") == fam]
        out += [_row_source(r, "web") for r in DQ.web_rows(fam, top=5)]
    out += [_row_source(r, "web") for r in DQ.material_rows(code, top=6)]
    seen: set[tuple] = set()
    uniq = []
    for r in sorted(out, key=lambda x: x["price"]):
        key = (r["supplier"], round(r["price"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq[:top]


def plan(components: dict[str, float], tons: float = 1.0, *, name: str = "") -> dict[str, Any]:
    """配方 → 采购方案：每种料用量、单价、来源、小计与合计。"""
    items = []
    total = 0.0
    missing = []
    for code, pct in sorted(components.items(), key=lambda kv: -kv[1]):
        if not pct:
            continue
        src = best_source(code)
        qty = tons * pct / 100.0
        sub = (src["price"] * qty) if src["price"] is not None else None
        if sub is None:
            missing.append(code)
        else:
            total += sub
        items.append({**src, "pct": pct, "qty_t": round(qty, 4), "subtotal": None if sub is None else round(sub),
                      "alternatives": alternatives(code)})
    return {"name": name or COST.format_components(components), "components": components, "tons": tons,
            "items": items, "total": None if missing else round(total), "cost_per_ton": None if missing else round(total / tons),
            "missing": missing, "at": db.now()}


def plan_for_scheme(rank: int = 1, tons: float = 1.0) -> dict[str, Any] | None:
    """最近一次推荐里第 rank 个方案的采购方案。"""
    r = db.q1("SELECT output_json FROM recommend_runs ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    out = db.loads(r["output_json"], {}) or {}
    for s in out.get("schemes") or []:
        if int(s.get("rank", 0)) == rank:
            return plan(s.get("components") or {}, tons, name=f"第 {rank} 名 · {s.get('formula')}")
    return None


def plan_for_code(code: str, tons: float = 1.0) -> dict[str, Any] | None:
    """配方库里某个编号的采购方案。"""
    from .formulation import library as L

    for f in L.list_formulations():
        if f["code"] == code:
            return plan(f["components"], tons, name=f"{code} · {f['formula']}")
    return None


def export(p: dict[str, Any], path: Path | None = None) -> Path:
    path = path or RFQ_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"代码": it["code"], "材料": it["name"], "比例%": it["pct"], f"用量(吨/{p['tons']:g}吨成品)": it["qty_t"],
             "单价(元/吨,到厂)": it["price"], "价格来源": TIER_LABEL.get(it["tier"], it["tier"]), "供应商 / 报价商": it["supplier"],
             "牌号 / 规格": it["grade"], "仓库地": it["region"], "交期": it["delivery"], "起订量": it["moq"],
             "联系方式": it["contact"], "报价日期": it["date"], "小计(元)": it["subtotal"], "来源链接": it["url"]}
            for it in p["items"]]
    alt_rows = [{"代码": it["code"], "材料": it["name"], "备选": i, "供应商": a["supplier"], "牌号/规格": a["grade"],
                 "到厂价": round(a["price"]), "地区": a["region"], "交期": a["delivery"], "起订量": a["moq"],
                 "联系方式": a["contact"], "报价日期": a["date"], "来源": a["url"]}
                for it in p["items"] for i, a in enumerate(it["alternatives"], 1)]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="采购清单", index=False)
        if alt_rows:
            pd.DataFrame(alt_rows).to_excel(xw, sheet_name="备选供应商", index=False)
        pd.DataFrame([{"项": "配方", "值": p["name"]}, {"项": "批量(吨)", "值": p["tons"]},
                      {"项": "合计(元)", "值": p["total"]}, {"项": "折合(元/吨)", "值": p["cost_per_ton"]},
                      {"项": "缺价格的料", "值": ", ".join(p["missing"]) or "无"}, {"项": "生成时间", "值": p["at"]},
                      {"项": "说明", "值": "单价为到厂含税口径（含税报价 + 运费表）；网查价未经询价核实，下单前请电话确认"}]
                     ).to_excel(xw, sheet_name="口径", index=False)
    return path


def text(p: dict[str, Any], max_alt: int = 1) -> str:
    """给对话用的采购说明。"""
    lines = [f"采购方案（{p['name']}，按 {p['tons']:g} 吨成品）："]
    for it in p["items"]:
        price = f"{it['price']:.0f}" if it["price"] is not None else "待询价"
        who = it["supplier"] or "（未找到供应商）"
        extra = "，".join(x for x in [it["grade"], it["region"], it["delivery"], (f"起订 {it['moq']}" if it["moq"] else ""),
                                     (f"电话 {it['contact']}" if it["contact"] else "")] if x)
        lines.append(f"- {it['name']}（{it['code']}）{it['pct']:g}% ≈ {it['qty_t']:.3g} 吨：{price} 元/吨"
                     f"（{TIER_LABEL.get(it['tier'], it['tier'])}，{who}{'，' + extra if extra else ''}）"
                     + (f"，小计 {it['subtotal']:,} 元" if it["subtotal"] else ""))
        for a in it["alternatives"][1:1 + max_alt]:
            lines.append(f"    备选：{a['supplier']} {a['price']:.0f}（{a['region'] or '地区不详'}{'，' + a['delivery'] if a['delivery'] else ''}）")
    if p["total"] is not None:
        lines.append(f"合计 {p['total']:,} 元，折合 {p['cost_per_ton']:,} 元/吨。")
    if p["missing"]:
        lines.append(f"缺价格：{', '.join(p['missing'])}——先在「供应商与报价」网查或直接询价。")
    lines.append("单价为到厂含税口径（含税报价 + 运费表）；网查价未经询价核实，下单前请电话确认。")
    return "\n".join(lines)


def summary_of_sources() -> str:
    """各材料当前的价格来源一览（给页面/对话说明“这些料现在都有谁在供”）。"""
    lines = []
    for m in MAT.list_materials(active_only=True):
        s = best_source(m["code"])
        if s["tier"] == "none":
            continue
        lines.append(f"{m['code']} {m['name']}：{s['price']:.0f} 元/吨（{TIER_LABEL[s['tier']]}"
                     + (f"，{s['supplier']}" if s["supplier"] else "") + "）")
    return "\n".join(lines) or "暂无价格来源。"
