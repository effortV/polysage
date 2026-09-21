"""价格预判：期货（大商所 L 线性主连）+ 我们自己的现货报价历史 + 近一周行情评述 → 每类树脂 1 周 / 1 月的价格预期，
并算出各候选配方在预期价格下的成本变化，帮助选“涨价也扛得住”的配方。

数据：
- 期货：新浪财经日 K 线接口（L0 主连；L 次主力合约看期限结构），国内直连可用。
- 现货：supplier_quotes 里日报 / 网查每天的最低到厂价。
- 评述：网页搜索近一周的 PE 行情周评/日评，抓正文给模型。
预判 = 模型给的方向与幅度（读了上面三类材料）与期货动量按 6:4 混合，幅度限在 ±10%/月。只是参考，不是交易建议。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from . import db, llm, net
from .formulation import cost as COST
from .formulation import materials as MAT

FAMILY_CODES = {"LLDPE": ["LL", "LLC"], "LDPE": ["LD"], "HDPE": ["HD"]}
FAMILY_LABEL = {"LLDPE": "线性 LLDPE", "LDPE": "高压 LDPE", "HDPE": "低压 HDPE"}
FAMILY_CN = {"LLDPE": "线性低密度聚乙烯 LLDPE", "LDPE": "高压低密度聚乙烯 LDPE", "HDPE": "低压高密度聚乙烯 HDPE"}
SINA_KLINE = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/InnerFuturesNewService.getDailyKLine?symbol={symbol}"
_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}
MAX_PCT_MONTH = 10.0

PROMPT = (
    "你是聚乙烯原料采购分析师。根据下面材料，对【{family_cn}】华东现货价格给出未来 1 周和 1 个月的预判。输出 JSON："
    "{{\"direction\": \"涨|跌|震荡\", \"pct_1w\": 数字（百分比，-5~5）, \"pct_1m\": 数字（百分比，-10~10）, \"confidence\": 0~1, "
    "\"drivers\": [{{\"text\": \"一句话依据\", \"url\": \"来源链接或空\"}}], \"advice\": \"对配方选择的一句建议（如：线性看涨，优先再生料/HDPE 比例高的方案）\"}}。"
    "只依据给定材料，不要编造数据；材料不足时 confidence 给低、幅度给小。\n\n"
    "当前价格卡到厂价：{current} 元/吨\n\n期货（大商所 L 主连，元/吨）：{futures}\n\n我们的现货报价历史（每日最低到厂价）：{spot}\n\n近一周行情评述摘录：\n{news}"
)


# ---------------- 期货 ----------------

def fetch_kline(symbol: str = "L0", days: int = 90) -> list[dict[str, Any]]:
    url = SINA_KLINE.format(symbol=symbol)
    with net.client(url, timeout=25) as c:
        r = c.get(url, headers=_HEADERS)
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    arr = json.loads(m.group(1)) if m else []
    out = []
    for k in arr[-days:]:
        try:
            out.append({"d": k["d"], "close": float(k["c"]), "settle": float(k.get("s") or k["c"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def next_contracts(today: date | None = None, n: int = 2) -> list[str]:
    """L 的活跃合约是 1/5/9 月：取今天之后最近的 n 个，如 2026-09-21 → L2701, L2705。"""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(12):
        m += 1
        if m > 12:
            m, y = 1, y + 1
        if m in (1, 5, 9):
            out.append(f"L{str(y)[2:]}{m:02d}")
            if len(out) == n:
                break
    return out


def futures_signal() -> dict[str, Any]:
    """主连动量 + 期限结构。"""
    main = fetch_kline("L0", 90)
    if len(main) < 25:
        return {"ok": False, "reason": "期货数据不足"}
    closes = [k["close"] for k in main]
    last = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    chg_5d = (last / closes[-6] - 1) * 100
    chg_20d = (last / closes[-21] - 1) * 100
    curve = {}
    for sym in next_contracts():
        try:
            k = fetch_kline(sym, 5)
            if k:
                curve[sym] = k[-1]["close"]
        except Exception:  # noqa: BLE001
            continue
    syms = list(curve)
    term = ((curve[syms[1]] / curve[syms[0]] - 1) * 100) if len(syms) >= 2 and curve[syms[0]] else None
    # 期货隐含的“1 月幅度”：20 日动量的一半 + 期限结构按 4 个月折到 1 个月
    implied_1m = 0.5 * chg_20d + (term / 4 if term is not None else 0.0)
    return {"ok": True, "date": main[-1]["d"], "last": last, "ma5": round(ma5), "ma20": round(ma20), "chg_5d": round(chg_5d, 2),
            "chg_20d": round(chg_20d, 2), "curve": curve, "term_pct": None if term is None else round(term, 2),
            "implied_1m": round(max(-MAX_PCT_MONTH, min(MAX_PCT_MONTH, implied_1m)), 2),
            "series": [(k["d"], k["close"]) for k in main[-60:]]}


# ---------------- 现货与评述 ----------------

def spot_history(family: str, days: int = 60) -> list[dict[str, Any]]:
    since = (date.today() - timedelta(days=days)).isoformat()
    return db.q("SELECT quote_date AS d, MIN(landed_price) AS landed, channel FROM supplier_quotes WHERE family=? AND status!='不可信' "
                "AND quote_date>=? AND landed_price IS NOT NULL GROUP BY quote_date ORDER BY quote_date", (family, since))


def market_news(family: str, limit: int = 3) -> list[dict[str, str]]:
    """近一周的行情评述：搜索 → 抓正文 → 取前 1500 字。"""
    from .sources.fetch import fetch_url
    from .sources.websearch import search

    fam_cn = {"LLDPE": "线性 LLDPE", "LDPE": "高压 LDPE", "HDPE": "低压 HDPE"}[family]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for q in (f"聚乙烯 {fam_cn} 行情 周评 预计 后市", f"PE {fam_cn} 华东 市场 分析 涨跌"):
        try:
            hits = search(q, limit=6, bing_first=True, timelimit="w")
        except Exception:  # noqa: BLE001
            continue
        for h in hits:
            if len(out) >= limit or not h.url or h.url in seen:
                continue
            seen.add(h.url)
            try:
                page = fetch_url(h.url)
            except Exception:  # noqa: BLE001
                continue
            text = (page.get("text") or "").strip()
            pd_ = page.get("date") or ""
            if len(text) < 300:
                continue
            if pd_ and not _within(pd_, 7):
                continue
            out.append({"title": (page.get("title") or h.title or "")[:80], "url": h.url, "date": pd_, "text": text[:1500]})
        if len(out) >= limit:
            break
    return out


def _within(date_str: str, days: int) -> bool:
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", date_str or "")
    if not m:
        return False
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return 0 <= (date.today() - d).days <= days


# ---------------- 预判 ----------------

def _current_price(family: str) -> float | None:
    codes = FAMILY_CODES.get(family) or []
    p = MAT.current_prices().get(codes[0], {}) if codes else {}
    return p.get("actual") if p.get("actual") is not None else p.get("estimate")


def build(family: str, *, futures: dict[str, Any] | None = None, news: list[dict[str, str]] | None = None) -> dict[str, Any]:
    futures = futures if futures is not None else _safe_futures()
    news = news if news is not None else market_news(family)
    current = _current_price(family)
    spot = spot_history(family)
    fut_txt = ("最新 {last}（{date}），5 日 {chg_5d:+.1f}%，20 日 {chg_20d:+.1f}%，MA5 {ma5} / MA20 {ma20}，期限结构 {term}，期货隐含 1 月 {implied_1m:+.1f}%"
               .format(term=(f"{futures['term_pct']:+.1f}%（远月-近月）" if futures.get("term_pct") is not None else "无"), **futures)
               if futures.get("ok") else f"不可用（{futures.get('reason')}）")
    spot_txt = "；".join(f"{r['d']} {r['landed']:.0f}" for r in spot[-15:]) or "无"
    news_txt = "\n".join(f"[{i + 1}] {n['title']}（{n['date'] or '日期不详'}，{n['url']}）：{n['text'][:700]}" for i, n in enumerate(news)) or "无"
    try:
        data = llm.chat_json([{"role": "system", "content": "你是聚乙烯原料采购分析师，输出严格 JSON。"},
                              {"role": "user", "content": PROMPT.format(family_cn=FAMILY_CN[family], current=f"{current:.0f}" if current else "未知",
                                                                        futures=fut_txt, spot=spot_txt, news=news_txt)}], max_tokens=1500, thinking=1024)
    except Exception as e:  # noqa: BLE001
        data = {"direction": "震荡", "pct_1w": 0, "pct_1m": 0, "confidence": 0.1, "drivers": [{"text": f"模型不可用：{e}", "url": ""}], "advice": ""}

    def _f(v: Any, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return 0.0

    llm_1m = _f(data.get("pct_1m"), -MAX_PCT_MONTH, MAX_PCT_MONTH)
    llm_1w = _f(data.get("pct_1w"), -5, 5)
    fut_1m = futures.get("implied_1m", 0.0) if futures.get("ok") else llm_1m
    pct_1m = round(0.6 * llm_1m + 0.4 * fut_1m, 2)
    pct_1w = round(0.6 * llm_1w + 0.4 * (fut_1m / 4), 2)
    direction = "涨" if pct_1m > 1 else ("跌" if pct_1m < -1 else "震荡")
    rec = {
        "at": db.now(), "family": family, "current": current,
        "pct_1w": pct_1w, "pct_1m": pct_1m,
        "price_1w": round(current * (1 + pct_1w / 100)) if current else None,
        "price_1m": round(current * (1 + pct_1m / 100)) if current else None,
        "direction": direction, "confidence": _f(data.get("confidence"), 0, 1),
        "drivers": [d for d in (data.get("drivers") or []) if isinstance(d, dict) and d.get("text")][:6],
        "advice": (data.get("advice") or "")[:200],
        "method": {"llm_pct_1m": llm_1m, "futures_implied_1m": fut_1m if futures.get("ok") else None, "blend": "0.6×模型 + 0.4×期货"},
        "futures": {k: v for k, v in futures.items() if k != "series"},
        "sources": [{"title": n["title"], "url": n["url"], "date": n["date"]} for n in news],
    }
    db.insert("price_forecasts", {"at": rec["at"], "family": family, "current": current, "price_1w": rec["price_1w"], "price_1m": rec["price_1m"],
                                  "pct_1w": pct_1w, "pct_1m": pct_1m, "direction": direction, "confidence": rec["confidence"],
                                  "drivers_json": db.dumps(rec["drivers"]), "sources_json": db.dumps(rec["sources"]), "method_json": db.dumps(rec["method"])})
    return rec


def _safe_futures() -> dict[str, Any]:
    try:
        return futures_signal()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)[:80]}


def run(families: list[str] | None = None, echo: Any = None) -> dict[str, dict[str, Any]]:
    """更新所有类别的预判（期货只取一次；三类的评述并行找）。"""
    from concurrent.futures import ThreadPoolExecutor

    families = families or list(FAMILY_CODES)
    futures = _safe_futures()
    with ThreadPoolExecutor(max_workers=3) as ex:
        news_all = dict(zip(families, ex.map(lambda f: market_news(f, limit=2), families)))
    out = {}
    for fam in families:
        out[fam] = build(fam, futures=futures, news=news_all.get(fam) or [])
        if echo:
            echo(f"{FAMILY_LABEL[fam]}：1 月 {out[fam]['pct_1m']:+.1f}%（{out[fam]['direction']}，置信 {out[fam]['confidence']:.0%}）")
    return out


def latest() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for fam in FAMILY_CODES:
        r = db.q1("SELECT * FROM price_forecasts WHERE family=? ORDER BY id DESC LIMIT 1", (fam,))
        if r:
            r["drivers"] = db.loads(r.get("drivers_json"), [])
            r["sources"] = db.loads(r.get("sources_json"), [])
            r["method"] = db.loads(r.get("method_json"), {})
            out[fam] = r
    return out


def outlook_text() -> str:
    lt = latest()
    if not lt:
        return "尚无价格预判（到“供应商与报价 → 价格预判”点更新）。"
    lines = [f"价格预判（{next(iter(lt.values()))['at'][:16].replace('T', ' ')}）："]
    for fam, r in lt.items():
        cur = f"{r['current']:.0f}" if r.get("current") else "?"
        lines.append(f"- {FAMILY_LABEL[fam]}：现 {cur} → 1 周 {r['price_1w'] or '?'}（{r['pct_1w']:+.1f}%）、1 月 {r['price_1m'] or '?'}（{r['pct_1m']:+.1f}%），"
                     f"{r['direction']}，置信 {r['confidence']:.0%}")
        for d in (r.get("drivers") or [])[:2]:
            lines.append(f"    依据：{d.get('text')}" + (f"（{d.get('url')}）" if d.get("url") else ""))
    return "\n".join(lines)


# ---------------- 对配方的影响 ----------------

def scenario_prices(horizon: str = "1m") -> dict[str, float]:
    """预期价格表：{材料代码: 预期到厂价}，只含有预判的类别。"""
    out: dict[str, float] = {}
    for fam, r in latest().items():
        p = r.get("price_1m" if horizon == "1m" else "price_1w")
        if p:
            for code in FAMILY_CODES[fam]:
                out[code] = float(p)
    return out


def scheme_impact(top_n: int = 12, horizon: str = "1m") -> list[dict[str, Any]]:
    """最近一次推荐的方案（没有就用配方库）在预期价格下的成本变化，按预期成本升序。"""
    scen = scenario_prices(horizon)
    if not scen:
        return []
    mats = COST._materials_map()
    mats_f = {k: dict(v) for k, v in mats.items()}
    for code, p in scen.items():
        if code in mats_f:
            mats_f[code]["price_actual"] = p
    run_row = db.q1("SELECT output_json FROM recommend_runs ORDER BY id DESC LIMIT 1")
    schemes: list[dict[str, Any]] = []
    if run_row:
        out = db.loads(run_row["output_json"], {}) or {}
        for s in (out.get("schemes") or [])[:top_n]:
            schemes.append({"name": s.get("formula") or s.get("theme"), "components": s.get("components") or {}, "theme": s.get("theme", "")})
        base = out.get("base_formulation")
        if isinstance(base, dict):
            schemes.insert(0, {"name": "现配方（base）", "components": base, "theme": "base"})
    if not schemes:
        from .formulation import library as L

        for f in L.list_formulations()[:top_n]:
            comps = db.loads(f.get("components_json"), {}) or {}
            schemes.append({"name": f.get("code"), "components": comps, "theme": f.get("rationale", "")[:30]})
    rows = []
    for s in schemes:
        now = COST.compute_simple(s["components"], mats)
        fut = COST.compute_simple(s["components"], mats_f)
        if now is None or fut is None:
            continue
        rows.append({"name": s["name"], "theme": s["theme"], "cost_now": round(now), "cost_future": round(fut), "delta": round(fut - now),
                     "delta_pct": round((fut / now - 1) * 100, 1), "components": s["components"]})
    base_now = rows[0]["cost_now"] if rows and rows[0]["theme"] == "base" else None
    for r in rows:
        r["savings_future"] = (base_now and rows[0]["cost_future"] - r["cost_future"]) or None
    rows.sort(key=lambda r: (r["theme"] != "base", r["cost_future"]))
    return rows
