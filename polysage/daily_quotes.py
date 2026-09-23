"""贸易商日报：把微信里的文字报价 / 截图解析成结构化报价，折算到厂价，按“每类树脂选最便宜”更新价格卡。

一条报价 = 树脂类别（LLDPE/LDPE/HDPE/mLLDPE）+ 厂家 + 牌号 + 仓库地 + 货物状态（现货/在途/计划）+ 含税价（H）+ 报价商 + 日期。
到厂价 = 含税价 + 运费（运费表按仓库地匹配，可在界面改）。
选用规则（用户定）：LLDPE / LDPE / HDPE 各自牌号差不多，每类取当日最低到厂价，写进价格卡的实价，推荐方案自动重排。
"""
from __future__ import annotations

import re
from datetime import date as _date
from typing import Any
from urllib.parse import urlparse

from . import activity, db, kb, llm, pricing, sourcing
from .config import KB
from .formulation import materials as MAT

FREIGHT_PATH = KB["price"] / "运费表.yaml"

FAMILIES = ["LLDPE", "LDPE", "HDPE", "mLLDPE", "其他"]
# 每类树脂对应价格卡里的材料代码（同类牌号视为等价，最低价同时写给这些代码）
FAMILY_CODES: dict[str, list[str]] = {"LLDPE": ["LL", "LLC"], "LDPE": ["LD"], "HDPE": ["HD"], "mLLDPE": ["mLL"]}
FAMILY_LABEL = {"LLDPE": "线性 LLDPE", "LDPE": "高压 LDPE", "HDPE": "低压 HDPE", "mLLDPE": "茂金属 mLLDPE", "其他": "其他"}

# 运费默认表（元/吨，仓库地 → 到厂）；键按子串匹配，越靠前越优先
DEFAULT_FREIGHT: dict[str, float] = {
    "上海": 100, "杭州": 100, "宁波": 100,
    "太仓": 150, "嘉兴": 120, "绍兴": 120, "湖州": 130, "苏州": 150, "余姚": 110,
    "金华": 200, "台州": 200, "温州": 220, "义乌": 200,
    "浙江": 150, "江苏": 200, "安徽": 250,
    "青岛": 350, "山东": 350, "广东": 400, "华南": 400, "华北": 400,
    "默认": 150,  # 两家报价商都在浙江，没写仓库地的按省内近距离算
}
# 仓库地优先级（价格相同时）：上海/杭州/宁波 最优
REGION_RANK = [("上海", 0), ("杭州", 0), ("宁波", 0), ("太仓", 1), ("嘉兴", 1), ("绍兴", 1), ("湖州", 1), ("苏州", 1), ("余姚", 1),
               ("金华", 2), ("台州", 2), ("温州", 2), ("浙江", 2), ("江苏", 2)]
DELIVERY_RANK = [("码头现货", 0), ("现货", 0), ("在途", 1), ("本周", 2), ("开单", 2), ("计划", 2), ("提货", 2), ("下周", 3), ("月底", 3), ("月", 3)]

PARSE_PROMPT = (
    "下面是塑料贸易商发来的当日报价（微信文字或截图 OCR 文本），请拆成结构化行。输出 JSON："
    "{{\"quotes\": [{{\"family\": \"LLDPE|LDPE|HDPE|mLLDPE|其他\", \"producer\": \"生产厂家（如 浙石化/宝来/裕龙/镇海/大庆/独山子/泉化/万华/乐天/华泰）\", "
    "\"grade\": \"牌号（如 7042/7047/2426H/6098/TR144/5110）\", \"warehouse\": \"仓库地/港口（如 杭州/上海/太仓/宁波，没有留空）\", "
    "\"delivery\": \"货物状态（现货/码头现货/在途/本周计划/开单计划/月底/9月22起提货 等，原文）\", "
    "\"price\": 数字（元/吨）, \"tax_included\": true/false, \"note\": \"其他备注，如 +H、配送绍兴\"}}]}}。"
    "规则：价格后的 H 表示含税（tax_included=true），\"+H\" 也按含税并在 note 写 \"+H\"；没有 H 的按原文 tax_included=false；"
    "分类：线性/7042/7047/7050/0209/218W/DFDA → LLDPE；高压/2426H/2420D/2102 → LDPE；低压/6098/6097/6095H/5110/55110/144/TR144/7000F/5000S → HDPE；"
    "茂金属/1018/5400/Exceed/Elite/mPE → mLLDPE；其他归 其他。每行一条，不要合并，不要漏。\n\n报价商：{trader}\n日期：{date}\n原文：\n{text}"
)


# ---------------- 运费表 ----------------

def load_freight() -> dict[str, float]:
    data = kb.read_yaml(FREIGHT_PATH) if FREIGHT_PATH.exists() else None
    if isinstance(data, dict) and data:
        return {str(k): float(v) for k, v in data.items()}
    return dict(DEFAULT_FREIGHT)


def save_freight(table: dict[str, float]) -> None:
    clean = {str(k).strip(): float(v) for k, v in table.items() if str(k).strip()}
    if "默认" not in clean:
        clean["默认"] = DEFAULT_FREIGHT["默认"]
    kb.write_yaml(FREIGHT_PATH, clean)


def freight_for(warehouse: str, table: dict[str, float] | None = None, delivery: str = "") -> float:
    """按仓库地匹配运费；没写仓库地时看货物状态里有没有地名（如“配送绍兴”）；都没有用默认。"""
    table = table or load_freight()
    for text in (warehouse or "", delivery or ""):
        for key, val in table.items():
            if key != "默认" and key in text:
                return float(val)
    return float(table.get("默认", DEFAULT_FREIGHT["默认"]))


def region_rank(warehouse: str) -> int:
    for key, r in REGION_RANK:
        if key in (warehouse or ""):
            return r
    return 3


def delivery_rank(delivery: str) -> int:
    for key, r in DELIVERY_RANK:
        if key in (delivery or ""):
            return r
    return 2


# ---------------- 解析 ----------------

def _num(v: Any) -> float | None:
    try:
        p = float(re.sub(r"[^\d.]", "", str(v)))
    except ValueError:
        return None
    return p if 1000 <= p <= 100000 else None


def parse_text(text: str, trader: str, quote_date: str) -> list[dict[str, Any]]:
    """用模型把报价原文拆成行；每行补上到厂价。"""
    if not text.strip():
        return []
    data = llm.chat_json([{"role": "system", "content": "你是塑料原料采购助理，输出严格 JSON。"},
                          {"role": "user", "content": PARSE_PROMPT.format(trader=trader, date=quote_date, text=text[:6000])}],
                         max_tokens=3000)
    freight = load_freight()
    rows: list[dict[str, Any]] = []
    for q in (data.get("quotes") if isinstance(data, dict) else None) or []:
        price = _num(q.get("price"))
        if price is None:
            continue
        fam = q.get("family") if q.get("family") in FAMILIES else "其他"
        wh = (q.get("warehouse") or "").strip()[:30]
        fr = freight_for(wh, freight, q.get("delivery") or "")
        rows.append({
            "family": fam, "producer": (q.get("producer") or "").strip()[:30], "grade": (q.get("grade") or "").strip()[:30],
            "warehouse": wh, "delivery": (q.get("delivery") or "").strip()[:30], "price": price,
            "tax_included": bool(q.get("tax_included", True)), "note": (q.get("note") or "").strip()[:60],
            "freight": fr, "landed": price + fr, "trader": trader, "date": quote_date,
        })
    return rows


def ocr_image(image_bytes: bytes) -> str:
    """截图 → 文本行（rapidocr，离线）。未安装时抛错，界面提示改粘贴文字。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError("未安装 rapidocr-onnxruntime，无法识别截图；请粘贴文字，或 pip install rapidocr-onnxruntime") from e
    import numpy as np
    from PIL import Image
    import io

    img = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
    result, _ = RapidOCR()(img)
    if not result:
        return ""
    # 按行聚合：y 相近的框合并成一行，按 x 排序
    boxes = [(float(min(p[1] for p in box)), float(min(p[0] for p in box)), txt) for box, txt, _ in result]
    boxes.sort()
    lines: list[list[tuple[float, str]]] = []
    last_y = None
    for y, x, txt in boxes:
        if last_y is None or abs(y - last_y) > 12:
            lines.append([])
            last_y = y
        lines[-1].append((x, txt))
    return "\n".join(" | ".join(t for _, t in sorted(ln)) for ln in lines)


# ---------------- 入库 ----------------

def _code_for(family: str) -> str | None:
    codes = FAMILY_CODES.get(family) or []
    return codes[0] if codes else None


def save_rows(rows: list[dict[str, Any]], *, channel: str = "日报", credibility: int = 5) -> dict[str, int]:
    """入库到 supplier_quotes（报价商作为供应商）。channel=日报（贸易商发来的）/ 网查（对照）。返回 {新增, 重复, 跳过}。"""
    n_new = n_dup = n_skip = 0
    for r in rows:
        code = _code_for(r["family"])
        if not code:
            n_skip += 1
            continue
        sid = sourcing.upsert_supplier({"name": r["trader"], "kind": "贸易商" if channel == "日报" else "网查来源", "materials": code,
                                        "credibility": credibility, "status": channel})
        grade = f"{r['producer']} {r['grade']}".strip()   # 库里“牌号”带厂家，通用结果页也能看懂
        dup = db.q1("SELECT id FROM supplier_quotes WHERE supplier_id=? AND material_code=? AND IFNULL(grade,'')=? "
                    "AND IFNULL(warehouse,'')=? AND quote_date=? AND price=?",
                    (sid, code, grade, r["warehouse"], r["date"], r["price"]))
        if dup:
            n_dup += 1
            continue
        db.insert("supplier_quotes", {
            "supplier_id": sid, "material_code": code, "grade": grade, "price": r["price"], "unit": "元/吨",
            "basis": ("含税(H)" if r["tax_included"] else "不含税") + (f" {r['note']}" if r["note"] else ""), "moq": "",
            "quote_date": r["date"], "source_url": r.get("url") or f"贸易商日报：{r['trader']} {r['date']}",
            "evidence": f"{r['producer']} {r['grade']} {r['warehouse']} {r['delivery']} {r['price']:.0f}{'H' if r['tax_included'] else ''}",
            "credibility": credibility, "status": "网查", "in_rfq": 0, "actual_price": None, "note": "", "created_at": db.now(),
            "family": r["family"], "producer": r["producer"], "warehouse": r["warehouse"], "delivery": r["delivery"],
            "landed_price": r["landed"], "channel": channel,
        })
        n_new += 1
    return {"new": n_new, "dup": n_dup, "skip": n_skip}


def daily_rows(quote_date: str | None = None) -> list[dict[str, Any]]:
    """某天（默认最近一天）的日报报价，按到厂价升序。"""
    if not quote_date:
        r = db.q1("SELECT MAX(quote_date) AS d FROM supplier_quotes WHERE channel='日报'")
        quote_date = r["d"] if r else None
    if not quote_date:
        return []
    rows = db.q("SELECT q.*, s.name AS trader FROM supplier_quotes q JOIN suppliers s ON s.id=q.supplier_id "
                "WHERE q.channel='日报' AND q.quote_date=? AND q.status!='不可信' ORDER BY q.landed_price ASC, q.id", (quote_date,))
    for r in rows:
        r["region_rank"] = region_rank(r.get("warehouse") or "")
        r["delivery_rank"] = delivery_rank(r.get("delivery") or "")
    rows.sort(key=lambda r: (r["landed_price"] or 1e9, r["delivery_rank"], r["region_rank"]))
    return rows


def picks(quote_date: str | None = None, top: int = 3) -> dict[str, dict[str, Any]]:
    """每类树脂当日前 top 名 + 与上一次日报最低价的涨跌。"""
    rows = daily_rows(quote_date)
    if not rows:
        return {}
    day = rows[0]["quote_date"]
    prev = db.q1("SELECT MAX(quote_date) AS d FROM supplier_quotes WHERE channel='日报' AND quote_date<?", (day,))
    prev_rows = daily_rows(prev["d"]) if prev and prev.get("d") else []
    out: dict[str, dict[str, Any]] = {}
    for fam in FAMILIES:
        fr = [r for r in rows if r.get("family") == fam]
        if not fr:
            continue
        best = fr[0]
        prev_min = min((p["landed_price"] for p in prev_rows if p.get("family") == fam and p["landed_price"]), default=None)
        codes = FAMILY_CODES.get(fam) or []
        cur = MAT.current_prices().get(codes[0], {}) if codes else {}
        current = cur.get("actual") if cur.get("actual") is not None else cur.get("estimate")
        out[fam] = {"date": day, "best": best, "top": fr[:top], "prev_min": prev_min,
                    "change": (best["landed_price"] - prev_min) if prev_min else None,
                    "current_card": current, "codes": codes}
    return out


def apply_cheapest(quote_date: str | None = None) -> list[dict[str, Any]]:
    """每类取当日最低到厂价，写进价格卡实价（同类所有代码）。当天已经是同一价就不重复写。"""
    applied = []
    for fam, p in picks(quote_date).items():
        best = p["best"]
        landed = float(best["landed_price"])
        for code in p["codes"]:
            if not MAT.get_material(code):
                continue
            last = db.q1("SELECT price, price_date FROM prices WHERE material_code=? AND price_type='actual' ORDER BY id DESC LIMIT 1", (code,))
            if last and abs(float(last["price"]) - landed) < 0.5 and last.get("price_date") == best["quote_date"]:
                continue
            pricing.record_price(code, landed, "actual", supplier=best["trader"], price_date=best["quote_date"],
                                 source=f"贸易商日报 {best['trader']}：{best['grade']} {best['warehouse']} {best['delivery']} "
                                        f"含税 {best['price']:.0f} + 运费 {landed - best['price']:.0f}",
                                 note=f"按“{FAMILY_LABEL[fam]} 当日最低到厂价”自动选用")
            applied.append({"family": fam, "code": code, "landed": landed, "producer": best["producer"], "grade": best["grade"].replace(best["producer"], "").strip(),
                            "warehouse": best["warehouse"], "trader": best["trader"]})
    return applied


def history(family: str, days: int = 60) -> list[dict[str, Any]]:
    """每天最低到厂价（画趋势）。"""
    return db.q("SELECT quote_date AS d, MIN(landed_price) AS landed, MIN(price) AS price FROM supplier_quotes "
                "WHERE channel='日报' AND family=? AND status!='不可信' GROUP BY quote_date ORDER BY quote_date DESC LIMIT ?", (family, days))[::-1]


def import_text(text: str, trader: str, quote_date: str | None = None, *, apply: bool = True) -> dict[str, Any]:
    """一步到位：解析 → 入库 → 按最低价更新价格卡。对话工具与界面共用。"""
    quote_date = quote_date or _date.today().isoformat()
    rows = parse_text(text, trader, quote_date)
    saved = save_rows(rows)
    applied = apply_cheapest(quote_date) if apply and rows else []
    return {"rows": rows, "saved": saved, "applied": applied, "date": quote_date}


def picks_text(quote_date: str | None = None) -> str:
    ps = picks(quote_date)
    if not ps:
        return "还没有贸易商日报数据。"
    lines = [f"贸易商日报 {next(iter(ps.values()))['date']}，每类最低到厂价（含税价 + 运费）："]
    for fam, p in ps.items():
        b = p["best"]
        chg = "" if p["change"] is None else f"，比上次 {p['change']:+.0f}"
        card = f"，价格卡现价 {p['current_card']:.0f}" if p.get("current_card") else ""
        lines.append(f"- {FAMILY_LABEL[fam]}：{b['grade']} @ {b['warehouse'] or '未注明'}（{b['delivery']}）含税 {b['price']:.0f}，"
                     f"到厂 {b['landed_price']:.0f}，{b['trader']}{chg}{card}")
        for r in p["top"][1:]:
            lines.append(f"    次选：{r['grade']} @ {r['warehouse'] or '未注明'} 到厂 {r['landed_price']:.0f}（{r['delivery']}）")
    return "\n".join(lines)


# ---------------- 网查同类报价（对照，不自动进价格卡） ----------------

WEB_PROMPT = (
    "下面是一个塑料行情/报价网页的正文。请把其中【{family_cn}】（关键词：{keys}）的贸易商/市场报价拆成结构化行。输出 JSON："
    "{{\"quotes\": [{{\"family\": \"LLDPE|LDPE|HDPE|mLLDPE|其他\", \"producer\": \"生产厂家（浙石化/宝来/裕龙/镇海/大庆/独山子/泉化/万华/乐天/华泰/中沙/扬子/茂名…）\", "
    "\"grade\": \"牌号（7042/7047/2426H/6098/TR144/5110…）\", \"warehouse\": \"仓库地/市场（杭州/上海/宁波/太仓/华东…，没有留空）\", "
    "\"delivery\": \"货物状态（现货/在途/计划/期货…，没有留空）\", \"price\": 数字（元/吨）, \"tax_included\": true/false, "
    "\"seller\": \"报价方：贸易商名或市场名（如 生意社华东/塑料在线-某某塑化），没有留空\", \"date\": \"报价日期 YYYY-MM-DD，页面没写就留空\"}}]}}。"
    "只要人民币元/吨的报价，元/kg 换算；行情站的“市场价/主流价”也算一条（seller 写市场名，delivery 写 市场价）；"
    "日期尽量从页面上取（更新时间、发布时间），取不到留空。最多 20 条，优先华东（上海/杭州/宁波/江苏/浙江）与价格低的。\n\n网页：{url}\n正文：\n{text}"
)
# 默认目标（没有日报时的兜底）：厂家+牌号
DEFAULT_TARGETS: dict[str, list[tuple[str, str]]] = {
    "LLDPE": [("浙石化", "7042"), ("宝来", "7042"), ("裕龙", "7042"), ("华泰", "7042"), ("中沙", "7042"), ("扬子", "7042")],
    "LDPE": [("浙石化", "2426H"), ("扬子", "2426H"), ("茂名", "2426H")],
    "HDPE": [("镇海", "6098"), ("大庆", "6097"), ("裕龙", "TR144"), ("独山子", "6095H")],
    "mLLDPE": [("埃克森", "1018"), ("陶氏", "5400G")],
}
FAMILY_CN = {"LLDPE": "线性低密度聚乙烯 LLDPE", "LDPE": "高压低密度聚乙烯 LDPE", "HDPE": "低压高密度聚乙烯 HDPE", "mLLDPE": "茂金属 mLLDPE"}
WEB_MAX_AGE_DAYS = 7  # 用户要求：网查只要近一周的


def seed_targets(family: str) -> list[tuple[str, str]]:
    """从最近的日报里取该类的 厂家+牌号 作检索目标；没有就用默认表。"""
    rows = db.q("SELECT DISTINCT producer, grade FROM supplier_quotes WHERE channel='日报' AND family=? AND status!='不可信' "
                "ORDER BY quote_date DESC LIMIT 12", (family,))
    out = []
    for r in rows:
        g = (r.get("grade") or "").replace(r.get("producer") or "", "").strip()
        if r.get("producer") and g:
            out.append((r["producer"], g))
    return out or DEFAULT_TARGETS.get(family, [])


def web_queries(family: str, depth: str = "标准") -> list[str]:
    targets = seed_targets(family)
    grades = list(dict.fromkeys(g for _, g in targets))
    fam_cn = {"LLDPE": "线性", "LDPE": "高压", "HDPE": "低压", "mLLDPE": "茂金属"}.get(family, family)
    qs: list[str] = []
    for p, g in targets[:4]:
        qs.append(f"{p}{g} 报价 华东")
    for g in grades[:3]:
        qs.append(f"{g} 杭州 现货 报价")
        qs.append(f"site:plasway.com {g}")
    qs.append(f"PE {fam_cn} 华东 市场报价 今日 贸易商")
    qs.append(f"site:100ppi.com {family}")
    n = {"快": 3, "标准": 6, "深": 10}.get(depth, 6)
    return list(dict.fromkeys(qs))[:n]


def _fresh(date_str: str, today: _date, max_age: int | None = None) -> bool:
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", date_str or "")
    if not m:
        return False
    try:
        d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return 0 <= (today - d).days <= (max_age or WEB_MAX_AGE_DAYS)


def extract_web_quotes(family: str, url: str, text: str, today: _date, page_date: str = "") -> list[dict[str, Any]]:
    """page_date：页面的发布/更新日期（抓取时从 meta 取），报价行没写日期时用它。"""
    keys = "、".join(f"{p}{g}" for p, g in seed_targets(family)[:6])
    try:
        data = llm.chat_json([{"role": "system", "content": "你是塑料原料采购助理，输出严格 JSON。"},
                              {"role": "user", "content": WEB_PROMPT.format(family_cn=FAMILY_CN.get(family, family), keys=keys, url=url, text=text[:8000])}],
                             max_tokens=4000)
    except Exception as e:  # noqa: BLE001
        sourcing._log(f"网查抽取失败 {url[:60]}：{e}")
        return []
    freight = load_freight()
    host = urlparse(url).netloc.replace("www.", "")
    rows = []
    for q in (data.get("quotes") if isinstance(data, dict) else None) or []:
        price = _num(q.get("price"))
        fam = q.get("family") if q.get("family") in FAMILIES else "其他"
        if price is None or fam != family:
            continue
        date_str = (q.get("date") or "").strip()
        if not _fresh(date_str, today):
            date_str = page_date
        if not _fresh(date_str, today):
            continue  # 没日期或超过一周的不要：过期报价比没有还坏
        wh = (q.get("warehouse") or "").strip()[:30]
        seller = (q.get("seller") or "").strip()[:40] or host
        rows.append({"family": fam, "producer": (q.get("producer") or "").strip()[:30], "grade": (q.get("grade") or "").strip()[:30],
                     "warehouse": wh, "delivery": (q.get("delivery") or "").strip()[:30], "price": price,
                     "tax_included": bool(q.get("tax_included", True)), "note": "", "freight": freight_for(wh, freight),
                     "landed": price + freight_for(wh, freight), "trader": f"{seller}（{host}）" if host not in seller else seller,
                     "date": re.sub(r"[/.年月]", "-", date_str).rstrip("日-")[:10], "url": url})
    return rows


def web_market_quotes(families: list[str] | None = None, *, depth: str = "标准", pages_per_query: int = 3,
                      echo: Any = None) -> dict[str, Any]:
    """按日报同样的口径网查同类报价（厂家+牌号+仓库地+状态+含税价），入库 channel=网查，只作对照。"""
    from .sources.fetch import fetch_url
    from .sources.websearch import search as web_search

    families = families or ["LLDPE", "LDPE", "HDPE"]
    today = _date.today()
    summary: dict[str, Any] = {}
    activity.note(f"网查同类报价：{'、'.join(families)}")
    for fam in families:
        seen: set[str] = set()
        pages: list[tuple[str, str]] = []
        for q in web_queries(fam, depth):
            try:
                hits = web_search(q, limit=8, bing_first=True, timelimit="w")
            except Exception as e:  # noqa: BLE001
                sourcing._log(f"[{fam}] 搜索失败「{q}」：{str(e)[:80]}", echo)
                continue
            picked = stale = 0
            for h in hits:
                if picked >= pages_per_query or not h.url or h.url in seen:
                    continue
                seen.add(h.url)
                try:
                    page = fetch_url(h.url)
                except Exception:  # noqa: BLE001
                    continue
                text = page.get("text") or ""
                if len(text) < 200:
                    continue
                page_date = page.get("date") or ""
                if page_date and not _fresh(page_date, today):
                    stale += 1  # 页面本身超过一周，整页跳过，省一次模型调用
                    continue
                picked += 1
                pages.append((h.url, text, page_date))
            if stale:
                sourcing._log(f"[{fam}]「{q}」跳过 {stale} 个一周前的旧页面", echo)
        sourcing._log(f"[{fam}] 网查：{len(pages)} 个近期页面，开始抽取", echo)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(lambda p: extract_web_quotes(fam, p[0], p[1], today, p[2]), pages))
        rows = [r for rs in results for r in rs]
        saved = save_rows(rows, channel="网查", credibility=3)
        best = min(rows, key=lambda r: r["landed"]) if rows else None
        sourcing._log(f"[{fam}] 网查完成：{len(rows)} 条近一周报价，新增 {saved['new']}" +
                      (f"，最低到厂 {best['landed']:.0f}（{best['producer']} {best['grade']} @ {best['warehouse'] or '未注明'}，{best['trader']}）" if best else ""), echo)
        summary[fam] = {"pages": len(pages), "rows": len(rows), **saved, "best": best}
    db.insert("sourcing_runs", {"at": db.now(), "codes_json": db.dumps(families), "summary_json": db.dumps({k: {kk: vv for kk, vv in v.items() if kk != "best"} for k, v in summary.items()}),
                                "note": f"网查同类报价 depth={depth}"})
    return summary


def web_rows(family: str, days: int = WEB_MAX_AGE_DAYS, top: int = 5) -> list[dict[str, Any]]:
    """最近 days 天的网查报价，按到厂价升序。"""
    since = (_date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    rows = db.q("SELECT q.*, s.name AS trader FROM supplier_quotes q JOIN suppliers s ON s.id=q.supplier_id "
                "WHERE q.channel='网查' AND q.family=? AND q.quote_date>=? AND q.status!='不可信' ORDER BY q.landed_price ASC, q.id DESC", (family, since))
    return rows[:top]


def purge_old_web_quotes() -> dict[str, int]:
    """清掉旧版寻源抓的泛报价（没有类别/到厂价那批）和没有报价的孤儿厂商。"""
    n_q = db.q1("SELECT COUNT(*) AS n FROM supplier_quotes WHERE IFNULL(channel,'网查')!='日报' AND family IS NULL")["n"]
    db.execute("DELETE FROM supplier_quotes WHERE IFNULL(channel,'网查')!='日报' AND family IS NULL")
    n_s = db.q1("SELECT COUNT(*) AS n FROM suppliers WHERE id NOT IN (SELECT DISTINCT supplier_id FROM supplier_quotes) AND status NOT IN ('日报','手动','已合作','已报价')")["n"]
    db.execute("DELETE FROM suppliers WHERE id NOT IN (SELECT DISTINCT supplier_id FROM supplier_quotes) AND status NOT IN ('日报','手动','已合作','已报价')")
    return {"quotes": n_q, "suppliers": n_s}


# ---------------- 辅料 / 再生料网查（按材料代码，不是树脂大类） ----------------

MATERIAL_PROMPT = (
    "下面是一个塑料原料供应/报价网页的正文。请把其中【{name}】（{hint}）的供应商报价拆成结构化行。输出 JSON："
    "{{\"quotes\": [{{\"supplier\": \"厂商/店铺名称\", \"kind\": \"生产商|回收厂|贸易商|平台商家|行情|未知\", \"region\": \"省/市\", "
    "\"grade\": \"牌号/规格/等级（如 一级透明、VA 18%、PPA 母粒）\", \"price\": 数字（元/吨）, \"tax_included\": true/false, "
    "\"moq\": \"起订量\", \"contact\": \"页面上的电话/联系人，没有留空\", \"date\": \"报价日期 YYYY-MM-DD，页面没写留空\", "
    "\"evidence\": \"原文报价句（不超过 60 字）\"}}]}}。"
    "只要人民币报价，元/kg 换算成元/吨（×1000）；与该材料不同类的（别的牌号/别的品种）不要；匿名商家（“供应商 11”）不要；"
    "最多 12 条，优先价格低、信息全的；找不到就 quotes=[]。\n\n网页：{url}\n正文：\n{text}"
)

# 非主料的检索词与说明；主料（LL/LLC/LD/HD/mLL）走贸易商日报与大类网查
MATERIAL_QUERIES: dict[str, tuple[str, list[str]]] = {
    "R1": ("再生高压一级透明 PE 颗粒，吹膜用", ["再生高压一级透明料 颗粒 厂家 报价", "再生LDPE 一级 透明 颗粒 价格 吹膜", "site:1688.com 再生高压 一级 透明 颗粒"]),
    "R2": ("再生高压二级 PE 颗粒", ["再生高压二级料 颗粒 价格 厂家", "再生LDPE 二级料 报价"]),
    "RL": ("再生线性一级料（工业膜回料造粒）", ["再生线性 LLDPE 一级 颗粒 厂家 报价", "工业膜 回料 造粒 线性 价格", "site:1688.com 再生线性 一级 颗粒"]),
    "PCR": ("混合 PE 消费后回料造粒 PCR", ["PCR PE 再生颗粒 价格 厂家", "消费后回收 聚乙烯 造粒 报价"]),
    "RHD": ("再生 HDPE 颗粒", ["再生HDPE 颗粒 价格 厂家", "再生高密度聚乙烯 颗粒 报价"]),
    "SC": ("自有边角料回粒（厂内回收，通常不外购）", ["薄膜 边角料 回粒 加工 价格"]),
    "POE": ("POE 乙烯-辛烯共聚弹性体（Engage 8150/8200 类）", ["POE 8150 价格 报价 厂家", "POE 弹性体 8200 现货 报价", "陶氏 POE 8150 华东 价格"]),
    "POP": ("POP 塑性体（Versify/Affinity 类）", ["POP 塑性体 聚烯烃 价格 报价"]),
    "VL": ("VLDPE / ULDPE 超低密度聚乙烯", ["VLDPE 超低密度聚乙烯 价格 报价", "ULDPE 现货 报价"]),
    "EVA": ("EVA 膜级，VA 5～9%", ["EVA 14-2 价格 报价 膜级", "EVA VA含量 6% 吹膜 价格 厂家"]),
    "EMA": ("EMA / EBA 丙烯酸酯共聚物", ["EMA 共聚物 价格 报价", "EBA 树脂 现货 价格"]),
    "OBC": ("OBC 烯烃嵌段共聚物（Infuse 类）", ["OBC Infuse 9107 价格 报价"]),
    "ION": ("离聚物 / EAA", ["离聚物 Surlyn 价格 报价", "EAA 树脂 价格"]),
    "FL": ("CaCO₃ 填充母料（碳酸钙含量 80%）", ["碳酸钙填充母料 80% 价格 厂家 吹膜", "site:1688.com 填充母料 碳酸钙 吹膜"]),
    "TFL": ("透明填充母料（细粒径 CaCO₃/BaSO₄）", ["透明填充母料 价格 厂家 吹膜"]),
    "TALC": ("滑石粉母料", ["滑石粉母料 价格 厂家"]),
    "AD": ("功能助剂母料：PPA 加工助剂 + 抗氧剂", ["PPA 加工助剂母粒 价格 厂家", "聚乙烯 抗氧剂母粒 价格 报价"]),
    "CP": ("相容剂 MAH-g-PE 马来酸酐接枝", ["马来酸酐接枝 PE 相容剂 价格 厂家"]),
    "NUC": ("PE 用成核 / 透明剂", ["聚乙烯 成核剂 透明剂 价格 厂家"]),
    "ADR": ("扩链剂（ADR / BASF Joncryl 类）", ["扩链剂 ADR 4368 价格 报价", "Joncryl 扩链剂 价格"]),
    "MD": ("MDPE 膜料", ["MDPE 膜料 价格 报价 华东"]),
    "LL6": ("C6 己烯共聚 LLDPE", ["己烯 LLDPE 价格 报价 华东"]),
    "mLL8": ("茂金属 C8 LLDPE（Elite 5400 类）", ["Elite 5400 价格 报价", "茂金属聚乙烯 C8 价格 华东"]),
}
MATERIAL_MAX_AGE_DAYS = 30   # 辅料报价更新慢：一个月内都算有效（主料仍是一周）


def material_queries(code: str, depth: str = "标准") -> list[str]:
    m = MAT.get_material(code) or {}
    hint, qs = MATERIAL_QUERIES.get(code, ("", []))
    if not qs:
        name = re.sub(r"[（(].*?[)）]", "", m.get("name") or code).strip()
        qs = [f"{name} 价格 厂家 报价", f"{name} 供应商 现货 报价"]
    n = {"快": 2, "标准": 3, "深": 5}.get(depth, 3)
    return qs[:n]


def extract_material_quotes(code: str, url: str, text: str, today: _date, page_date: str = "") -> list[dict[str, Any]]:
    m = MAT.get_material(code) or {"name": code}
    hint = MATERIAL_QUERIES.get(code, ("", []))[0] or (m.get("role") or "")
    try:
        data = llm.chat_json([{"role": "system", "content": "你是塑料原料采购助理，输出严格 JSON。"},
                              {"role": "user", "content": MATERIAL_PROMPT.format(name=m.get("name", code), hint=hint, url=url, text=text[:8000])}],
                             max_tokens=3000)
    except Exception as e:  # noqa: BLE001
        sourcing._log(f"[{code}] 抽取失败 {url[:50]}：{e}")
        return []
    freight = load_freight()
    host = urlparse(url).netloc.replace("www.", "")
    out = []
    for q in (data.get("quotes") if isinstance(data, dict) else None) or []:
        price = _to_price_any(q.get("price"))
        name = (q.get("supplier") or "").strip()[:60]
        if price is None or not sourcing._valid_supplier(name, q.get("kind") or "未知"):
            continue
        date_str = (q.get("date") or "").strip()
        if not _fresh(date_str, today, MATERIAL_MAX_AGE_DAYS):
            date_str = page_date
        if not _fresh(date_str, today, MATERIAL_MAX_AGE_DAYS):
            date_str = today.isoformat()   # 辅料页面多为长期挂牌，没日期按当天记，可信度低一档
        region = (q.get("region") or "").strip()[:30]
        fr = freight_for(region, freight)
        out.append({"code": code, "supplier": name, "kind": q.get("kind") if q.get("kind") in KINDS_MAT else "未知", "region": region,
                    "grade": (q.get("grade") or "").strip()[:40], "price": price, "tax_included": bool(q.get("tax_included", True)),
                    "moq": (q.get("moq") or "").strip()[:30], "contact": (q.get("contact") or "").strip()[:60],
                    "date": re.sub(r"[/.年月]", "-", date_str).rstrip("日-")[:10], "freight": fr, "landed": price + fr,
                    "evidence": (q.get("evidence") or "").strip()[:120], "url": url, "host": host})
    return out


KINDS_MAT = ["生产商", "回收厂", "贸易商", "平台商家", "行情", "未知"]


def _to_price_any(v: Any) -> float | None:
    """辅料价格跨度大（回料 4000 到助剂 8 万），只做元/kg 换算与合理区间校验。"""
    try:
        p = float(re.sub(r"[^\d.]", "", str(v)))
    except (TypeError, ValueError):
        return None
    if p < 100:
        p *= 1000
    return p if 300 <= p <= 300000 else None


def save_material_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    n_new = n_dup = 0
    for r in rows:
        sid = sourcing.upsert_supplier({"name": r["supplier"], "kind": r["kind"], "region": r["region"], "contact": r["contact"],
                                        "url": r["url"], "materials": r["code"], "credibility": 3, "status": "网查"})
        dup = db.q1("SELECT id FROM supplier_quotes WHERE supplier_id=? AND material_code=? AND price=? AND IFNULL(grade,'')=?",
                    (sid, r["code"], r["price"], r["grade"]))
        if dup:
            n_dup += 1
            continue
        db.insert("supplier_quotes", {
            "supplier_id": sid, "material_code": r["code"], "grade": r["grade"], "price": r["price"], "unit": "元/吨",
            "basis": ("含税" if r["tax_included"] else "不含税"), "moq": r["moq"], "quote_date": r["date"], "source_url": r["url"],
            "evidence": r["evidence"], "credibility": 3, "status": "网查", "in_rfq": 0, "actual_price": None, "note": "",
            "created_at": db.now(), "family": None, "producer": "", "warehouse": r["region"], "delivery": "",
            "landed_price": r["landed"], "channel": "网查",
        })
        n_new += 1
    return {"new": n_new, "dup": n_dup}


def web_material_quotes(codes: list[str] | None = None, *, depth: str = "标准", pages_per_query: int = 3,
                        echo: Any = None) -> dict[str, Any]:
    """辅料 / 再生料按材料代码网查厂商与报价（同样折到厂价），入库 channel=网查。"""
    from concurrent.futures import ThreadPoolExecutor

    from .sources.fetch import fetch_url
    from .sources.websearch import search as web_search

    codes = codes or [c for c in MATERIAL_QUERIES if (MAT.get_material(c) or {}).get("active", 1)]
    today = _date.today()
    summary: dict[str, Any] = {}
    activity.note(f"辅料寻源：{'、'.join(codes)}")
    for code in codes:
        m = MAT.get_material(code)
        if not m:
            continue
        seen: set[str] = set()
        pages: list[tuple[str, str, str]] = []
        for q in material_queries(code, depth):
            try:
                hits = web_search(q, limit=8, bing_first=True, timelimit="m")
            except Exception as e:  # noqa: BLE001
                sourcing._log(f"[{code}] 搜索失败「{q}」：{str(e)[:80]}", echo)
                continue
            picked = 0
            for h in hits:
                if picked >= pages_per_query or not h.url or h.url in seen:
                    continue
                seen.add(h.url)
                try:
                    page = fetch_url(h.url)
                except Exception:  # noqa: BLE001
                    continue
                text = page.get("text") or ""
                if len(text) < 200:
                    continue
                pd_ = page.get("date") or ""
                if pd_ and not _fresh(pd_, today, MATERIAL_MAX_AGE_DAYS * 6):   # 半年前的页面直接丢
                    continue
                picked += 1
                pages.append((h.url, text, pd_))
        sourcing._log(f"[{code}] {m['name']}：{len(pages)} 个页面，开始抽取", echo)
        with ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(lambda p: extract_material_quotes(code, p[0], p[1], today, p[2]), pages))
        rows = [r for rs in results for r in rs]
        saved = save_material_rows(rows)
        best = min(rows, key=lambda r: r["landed"]) if rows else None
        cur = MAT.current_prices().get(code, {})
        cur_price = cur.get("actual") if cur.get("actual") is not None else cur.get("estimate")
        sourcing._log(f"[{code}] 完成：{len(rows)} 条报价，新增 {saved['new']}" +
                      (f"，最低到厂 {best['landed']:.0f}（{best['supplier'][:14]}，{best['region'] or '地区不详'}）"
                       f"，价格卡 {cur_price:.0f}" if best and cur_price else ""), echo)
        summary[code] = {"name": m["name"], "pages": len(pages), "rows": len(rows), **saved, "best": best, "current": cur_price}
    db.insert("sourcing_runs", {"at": db.now(), "codes_json": db.dumps(codes),
                                "summary_json": db.dumps({k: {kk: vv for kk, vv in v.items() if kk != "best"} for k, v in summary.items()}),
                                "note": f"辅料网查 depth={depth}"})
    return summary


def material_rows(code: str, days: int = MATERIAL_MAX_AGE_DAYS, top: int = 8) -> list[dict[str, Any]]:
    import datetime as _dt

    since = (_date.today() - _dt.timedelta(days=days)).isoformat()
    rows = db.q("SELECT q.*, s.name AS supplier, s.kind, s.region, s.contact FROM supplier_quotes q JOIN suppliers s ON s.id=q.supplier_id "
                "WHERE q.material_code=? AND q.channel='网查' AND q.quote_date>=? AND q.status!='不可信' AND q.family IS NULL "
                "ORDER BY q.landed_price ASC, q.id DESC", (code, since))
    return rows[:top]


def apply_web_estimates(codes: list[str] | None = None) -> list[dict[str, Any]]:
    """把辅料网查的最低到厂价写成估计价（estimate，不是实价）；有实价的材料跳过。"""
    out = []
    for code in (codes or list(MATERIAL_QUERIES)):
        rows = material_rows(code, top=1)
        if not rows:
            continue
        cur = MAT.current_prices().get(code, {})
        if cur.get("actual") is not None:
            continue
        landed = float(rows[0]["landed_price"])
        if cur.get("estimate") is not None and abs(cur["estimate"] - landed) < 1:
            continue
        pricing.record_price(code, landed, "estimate", supplier=rows[0]["supplier"], price_date=rows[0]["quote_date"],
                             url=rows[0]["source_url"] or "",
                             source=f"网查最低到厂价：{rows[0]['supplier']}（{rows[0]['region'] or '地区不详'}）{rows[0]['price']:.0f} + 运费",
                             note="辅料网查，未询价核实")
        out.append({"code": code, "landed": landed, "supplier": rows[0]["supplier"]})
    return out
