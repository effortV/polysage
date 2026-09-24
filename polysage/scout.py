"""智能体找新料：按用途上网搜 → 抓页面 → 抽成材料卡 → 入原料库（带来源网址）。

原料库只预置现配方在用的 4 种料（LL / LD / HD / R1），别的候选料都得这样查出来：
每条都带发现它的那个网页，查到报价就顺手写成估计价（未询价核实），查不到价就先放着当候选。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from . import activity, db, llm
from . import pricing
from .formulation.materials import current_prices, list_materials, upsert_material

# 默认按“怎么把包装膜做便宜”分几个方向找；用户给了 goal 就按 goal 找
DEFAULT_GOALS: list[tuple[str, list[str]]] = [
    ("同类更便宜的 LLDPE", ["吹膜级 LLDPE 牌号 厂家 报价 华东", "LLDPE 7042 同类 牌号 价格 对比"]),
    ("再生 PE 颗粒", ["再生PE 颗粒 吹膜 一级料 厂家 报价", "再生LLDPE 颗粒 吹膜级 价格 厂家"]),
    ("低比例增韧树脂", ["茂金属 LLDPE 吹膜 牌号 价格 国产", "POE 弹性体 吹膜 增韧 价格 厂家"]),
    ("加工与功能助剂", ["吹膜 PPA 加工助剂 母粒 厂家 报价", "聚乙烯 开口爽滑母粒 吹膜 价格 厂家"]),
]

EXTRACT_PROMPT = """从下面的网页正文里找出“可以买来吹聚乙烯包装膜的原料”，输出 JSON。

只收真实在卖或有明确牌号的料；不要实验室材料、不要论文里的配方、不要整篇行情评论里没有具体料的段落。
每种料给：
- name：中文名称（如“国产茂金属 LLDPE”“再生高压一级透明料”）
- category：主体树脂 / 再生料 / 弹性体 / 助剂 / 填充母料 之一
- grade：牌号（没有写空字符串）
- producer：厂家或牌号所属公司（没有写空）
- is_recycled：1 或 0
- role：对吹膜的作用，一句话（例如“替代现用 LLDPE，价格更低”“低比例增韧，提升穿刺”）
- typical_min / typical_max：建议用量百分比范围（整数，拿不准给 0 和 30）
- effects：四项方向，键固定为 拉伸/撕裂/穿刺/热封，值只能是 ↑↑ ↑ ≈ ↓ ↓↓ 之一
- risk：主要风险，一句话
- price：元/吨的含税价，只有正文里明确写了才给数字，否则 null
- price_basis：价格出处原话（≤40 字），没有价就空字符串

网页标题：{title}
正文：
{text}

输出：{{"materials": [...]}}；正文里没有合格的料就输出 {{"materials": []}}。"""

CODE_HINT = {"主体树脂": "M", "再生料": "R", "弹性体": "E", "助剂": "AD", "填充母料": "F"}
# 每类料的合理价区间（元/吨）：行情表里的数字常常连在一起，没有这个尺子就会抽出 86000 的 LLDPE
PRICE_RANGE = {"主体树脂": (5000, 30000), "再生料": (1500, 15000), "弹性体": (8000, 45000),
               "助剂": (3000, 150000), "填充母料": (1000, 20000)}
# 实验室/论文里的东西不要：买不到
LAB_WORDS = ("实验室", "自制", "自行合成", "课题组", "小试制备", "中试制备", "专利实施例", "样品制备")
# 外观不符的料：包装膜要本色/透明，黑色杂色回料再便宜也不能用
BAD_LOOK = ("黑色", "黑颗粒", "杂色", "彩色", "花料", "灰色", "深色")


def _use_flag(name: str, role: str = "") -> str:
    text = f"{name} {role}"
    if any(w in text for w in BAD_LOOK):
        return "否（外观不符：包装膜要本色/透明）"
    return "待评估"


def _next_code(category: str, taken: set[str]) -> str:
    """给新料起一个没被占用的短代码：类别首字母 + 序号。"""
    head = CODE_HINT.get(category, "X")
    for i in range(1, 100):
        code = f"{head}{i}"
        if code not in taken:
            return code
    return f"X{len(taken) + 1}"


def _same_material(name: str, grade: str, existing: list[dict[str, Any]]) -> dict[str, Any] | None:
    """已经在库里了就别重复加：先看牌号，再看名字。"""
    g = re.sub(r"[^0-9A-Za-z]", "", grade or "").upper()
    for m in existing:
        mg = re.sub(r"[^0-9A-Za-z]", "", m.get("grade") or "").upper()
        if g and mg and (g in mg or mg in g):
            return m
        if name and m["name"] and (name in m["name"] or m["name"] in name):
            return m
    return None


def _clean(it: dict[str, Any]) -> dict[str, Any] | None:
    name = str(it.get("name") or "").strip()[:40]
    if not name or len(name) < 2:
        return None
    cat = str(it.get("category") or "").strip()
    if cat not in CODE_HINT:
        cat = "主体树脂" if "PE" in name.upper() else "助剂"
    eff = {k: v for k, v in (it.get("effects") or {}).items() if k in ("拉伸", "撕裂", "穿刺", "热封")
           and v in ("↑↑", "↑", "≈", "↓", "↓↓")}
    lo, hi = it.get("typical_min"), it.get("typical_max")
    try:
        lo, hi = max(0.0, float(lo or 0)), min(100.0, float(hi or 30))
    except (TypeError, ValueError):
        lo, hi = 0.0, 30.0
    price = it.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    lo_p, hi_p = PRICE_RANGE.get(cat, (1000, 90000))
    if price is not None and not (lo_p <= price <= hi_p):         # 不在这类料的常识区间里，多半是抽错了
        price = None
    if any(w in name for w in LAB_WORDS):
        return None
    if not (str(it.get("grade") or "").strip() or str(it.get("producer") or "").strip() or price):
        return None                      # 没牌号、没厂家、也没报价的，不算能买到的料
    return {"name": name, "category": cat, "grade": str(it.get("grade") or "")[:40],
            "producer": str(it.get("producer") or "")[:40], "is_recycled": 1 if it.get("is_recycled") else 0,
            "role": str(it.get("role") or "")[:120], "typical_min": lo, "typical_max": hi,
            "effects": eff, "risk": str(it.get("risk") or "")[:120], "price": price,
            "price_basis": str(it.get("price_basis") or "")[:80]}


def _extract(title: str, text: str) -> list[dict[str, Any]]:
    try:
        data = llm.chat_json([{"role": "system", "content": "你是材料库管理员，只输出 JSON。"},
                              {"role": "user", "content": EXTRACT_PROMPT.format(title=title[:80], text=text[:6000])}],
                             max_tokens=1600, thinking=False)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("materials") or [])[:6]:
        c = _clean(it) if isinstance(it, dict) else None
        if c:
            out.append(c)
    return out


def discover(goal: str = "", *, depth: str = "标准", max_new: int = 6,
             echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    """上网找新料并入库。goal 为空时按默认几个降本方向找。"""
    from .sources.fetch import fetch_url
    from .sources.websearch import search as web_search

    pages_per_query = {"快": 1, "标准": 2, "深": 3}.get(depth, 2)
    plans = [(goal, [goal, f"{goal} 厂家 报价"])] if goal.strip() else DEFAULT_GOALS
    if goal.strip() and depth == "快":
        plans = [(goal, [goal])]
    existing = list_materials(active_only=False)
    taken = {m["code"] for m in existing}
    added: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    notes: list[str] = []
    activity.note(f"智能体找新料：{goal or '默认降本方向'}")
    for theme, queries in plans:
        if len(added) >= max_new:
            break
        for q in queries:
            if len(added) >= max_new:
                break
            try:
                hits = web_search(q, limit=6, bing_first=True, timelimit="y")
            except Exception as e:  # noqa: BLE001
                notes.append(f"搜索失败「{q}」：{str(e)[:60]}")
                continue
            picked = 0
            for h in hits:
                if picked >= pages_per_query or not h.url or h.url in seen_urls:
                    continue
                seen_urls.add(h.url)
                page = {}
                try:
                    page = fetch_url(h.url)
                except Exception:  # noqa: BLE001
                    pass
                # 行情站的正文常被挡住（抓回来一堆导航），搜索接口给的摘要里反而是真数据
                text = ((h.abstract or "").strip() + "\n\n" + (page.get("text") or "")).strip()
                if len(text) < 200:
                    continue
                picked += 1
                cands = _extract(page.get("title") or h.title, text)
                # 一个页面里好几种料报同一个价，基本是把页面上那个唯一的价套给了所有牌号：这种价不要
                prices_seen = [c["price"] for c in cands if c["price"]]
                if len(prices_seen) > 1 and len(set(prices_seen)) == 1:
                    for c in cands:
                        c["price"] = None
                    notes.append(f"{(page.get('title') or h.title)[:24]}：几种料报同一个价，判为页面通用价，没有采用")
                for c in cands:
                    if len(added) >= max_new:
                        break
                    dup = _same_material(c["name"], c["grade"], existing)
                    if dup:
                        if c["price"] and current_prices().get(dup["code"], {}).get("actual") is None:
                            pricing.record_price(dup["code"], c["price"], "estimate", url=h.url, price_date=page.get("date") or date.today().isoformat(),
                                      source=f"智能体发现·{c['producer'] or h.title[:24]}：{c['price_basis'] or '网页报价'}",
                                      note="网查参考价，未询价核实")
                            notes.append(f"{dup['code']} 已在库，更新估计价 {c['price']:.0f}")
                        continue
                    code = _next_code(c["category"], taken)
                    taken.add(code)
                    upsert_material({"code": code, "category": c["category"], "name": c["name"], "grade": c["grade"],
                                     "producer": c["producer"], "is_recycled": c["is_recycled"], "role": c["role"],
                                     "typical_min": c["typical_min"], "typical_max": c["typical_max"],
                                     "effects": c["effects"], "risk": c["risk"],
                                     "use_flag": _use_flag(c["name"], c["role"]),
                                     "origin": f"智能体发现 {theme}", "source_url": h.url, "discovered_at": db.now(),
                                     "notes": f"{theme}｜来源：{(page.get('title') or h.title)[:60]}"})
                    if c["price"]:
                        pricing.record_price(code, c["price"], "estimate", url=h.url, price_date=page.get("date") or date.today().isoformat(),
                                  source=f"智能体发现·{c['producer'] or h.title[:24]}：{c['price_basis'] or '网页报价'}",
                                  note="网查参考价，未询价核实")
                    rec = {"code": code, "name": c["name"], "grade": c["grade"], "category": c["category"],
                           "price": c["price"], "url": h.url, "theme": theme}
                    added.append(rec)
                    existing.append({"code": code, "name": c["name"], "grade": c["grade"]})
                    if echo:
                        echo(f"新料 {code} {c['name']}（{c['grade'] or '无牌号'}）"
                             + (f"，网查价 {c['price']:.0f}" if c["price"] else "，暂无报价") + f"｜{h.url[:60]}")
    return {"added": added, "notes": notes, "goal": goal or "默认降本方向"}


def discovered(limit: int = 100) -> list[dict[str, Any]]:
    """智能体发现的料（原料库里带来源网址的那些）。"""
    rows = db.q("SELECT code, name, grade, category, origin, source_url, discovered_at FROM materials "
                "WHERE COALESCE(source_url,'') <> '' ORDER BY discovered_at DESC, id DESC")
    prices = current_prices()
    for r in rows:
        p = prices.get(r["code"], {})
        r["price"] = p.get("actual") if p.get("actual") is not None else p.get("estimate")
        r["price_tier"] = "实价" if p.get("actual") is not None else ("估计价" if p.get("estimate") is not None else "缺")
    return rows[:limit]
