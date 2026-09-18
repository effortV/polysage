"""供应商寻源：为原料库里的材料在网上找厂商/贸易商/回收厂的报价，与价格卡比较，生成询价清单；询价核实后登记为实价。

流程：材料 → 检索词（按牌号/名称/再生料模板）→ 搜索引擎 → 抓页面正文 → LLM 抽取多条报价 → 入库（suppliers / supplier_quotes）
→ 与现价比较 → 询价清单（xlsx）→ 用户填实际报价 → 登记实价（走 pricing.record_price，推荐方案自动联动）。
网查到的是挂牌价 / 平台标价，只作“该问谁、大致区间”，不当实价。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import pandas as pd

from . import activity, db, llm, pricing
from .config import DATA_DIR, KB
from .formulation import materials as MAT
from .sources.fetch import fetch_url
from .sources.websearch import search as web_search

LOG_PATH = DATA_DIR / "sourcing.log"
REPORT_PATH = KB["price"] / "供应商寻源报告.md"
RFQ_PATH = KB["price"] / "询价清单_供应商.xlsx"

KINDS = ["生产商", "贸易商", "回收厂", "平台商家", "行情", "未知"]
QUOTE_STATUS = ["网查", "询价中", "已报价", "已登记", "不可信"]

# 域名可信度：行情站 / 石化官网高，B2B 平台中，其他低
_DOMAIN_CRED = {
    "100ppi.com": 4, "plasway.com": 4, "21cp.com": 4, "oilchem.net": 4, "sci99.com": 4,
    "sinopec.com": 5, "petrochina.com.cn": 5, "cnpc.com.cn": 5,
    "1688.com": 3, "hc360.com": 3, "zhaosuliao.com": 3, "sumicheng.com": 3, "makepolo.com": 2, "china.cn": 2,
}
DEPTHS = {"快": 2, "标准": 4, "深": 6}

EXTRACT_PROMPT = (
    "从下面网页正文中提取材料【{name}】（关键词：{key}）的供应/报价信息，可能有多条（多个商家、多个牌号、多个市场）。"
    "输出 JSON：{{\"offers\": [{{\"supplier\": \"厂商/店铺/贸易商/市场名称\", \"kind\": \"生产商|贸易商|回收厂|平台商家|行情|未知\", "
    "\"region\": \"省/市\", \"grade\": \"牌号或规格\", \"price_rmb_per_ton\": 数字或null, \"basis\": \"口径：含税/不含税、出厂/到厂、现货/期货、平台标价\", "
    "\"moq\": \"起订量\", \"date\": \"YYYY-MM-DD 或 YYYY-MM 或空\", \"contact\": \"页面上的电话/联系人，没有留空\", \"evidence\": \"原文中的报价句子（不超过 60 字）\"}}]}}。"
    "只提取明确写出的、与该材料同类的人民币报价（外币报价跳过）；单位不是元/吨时换算（元/kg×1000）；行情站的市场价用市场名做 supplier、kind=行情；"
    "最多 15 条，优先价格低、信息全的；找不到就 offers=[]。\n\n网页：{url}\n正文：\n{text}"
)


# ---------------- 日志 ----------------

def _log(msg: str, echo: Callable[[str], None] | None = None) -> None:
    line = f"[{db.now()}] {msg}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if echo:
        echo(line)


def read_log(n: int = 80) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(encoding="utf-8").splitlines()[-n:]


# ---------------- 检索词 ----------------

_FAMILY = re.compile(r"(mLLDPE|LLDPE|LDPE|HDPE|MDPE|POE|EVA|PE)", re.I)


def keyword(m: dict[str, Any]) -> str:
    """材料的检索关键词：牌号（前面补上树脂类别，如 “LLDPE 7042”），没有牌号用去掉括注的名称。"""
    grade = (m.get("grade") or "").strip()
    name = m.get("name") or ""
    fam = _FAMILY.search(name)
    if grade and grade not in ("—", "-", "待甲方提供", "待确认"):
        first = re.split(r"[/、,，;；]", grade)[0].strip()
        if first:
            return first if _FAMILY.search(first) or not fam else f"{fam.group(1)} {first}"
    plain = re.sub(r"[（(].*?[)）]", "", name).replace("现用", "").strip()
    return plain or m.get("code", "")


def queries_for(m: dict[str, Any], depth: str = "标准", extra: str = "") -> list[str]:
    key = keyword(m)
    name = re.sub(r"[（(].*?[)）]", "", m.get("name") or "").replace("现用", "").strip() or key
    if m.get("is_recycled"):
        qs = [f"{name} 厂家 报价", "再生PE颗粒 一级 透明 吹膜 厂家 报价", "再生聚乙烯 膜级 颗粒 价格 供应商",
              "site:1688.com 再生 PE 颗粒 一级 透明", f"{name} 回收造粒厂 供应 华东", "再生LDPE 一级料 价格 行情"]
    else:
        qs = [f"{key} 价格 厂家 报价", f"{key} 供应商 现货 报价", f"{name} 生产厂家 直供 价格", f"site:1688.com {key} 价格",
              f"{key} 华东 市场价 今日", f"{key} 挂牌价 出厂价"]
    if extra.strip():
        qs.insert(1, f"{key} {extra.strip()}")
    return qs[:DEPTHS.get(depth, 4)]


# ---------------- 抽取与入库 ----------------

def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _credibility(url: str) -> int:
    host = _domain(url)
    for d, c in _DOMAIN_CRED.items():
        if host.endswith(d):
            return c
    return 2


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("（", "(").replace("）", ")")).strip()[:80]


_ANON = re.compile(r"^(供应商|商家|卖家|用户|会员)?\s*[#＃]?\d*$")
_REGIONS = {"华东", "华南", "华北", "华中", "西南", "西北", "东北", "山东", "江苏", "浙江", "广东", "上海", "福建", "河北", "河南", "四川", "湖北", "湖南",
            "安徽", "江西", "辽宁", "天津", "北京", "重庆", "陕西", "山西", "广西", "云南", "贵州", "黑龙江", "吉林", "内蒙古", "新疆", "海南", "甘肃"}


def _valid_supplier(name: str, kind: str) -> bool:
    """去掉行情站里被隐藏的匿名商家（“供应商 11”）、纯地区名、过短的名字；行情类允许地区名（如“华东市场”）。"""
    if not name or _ANON.match(name):
        return False
    if kind == "行情":
        return True
    return len(name) >= 3 and name not in _REGIONS


def _clean_basis(s: str) -> str:
    s = (s or "").strip()
    return "" if s in ("未知", "不详", "无", "null", "None", "-") else s[:60]


def _to_price(v: Any) -> float | None:
    try:
        p = float(str(v).replace(",", "").replace("元", "").strip())
    except (TypeError, ValueError):
        return None
    if p < 100:  # 写成 元/kg 的
        p *= 1000
    return p if 300 <= p <= 200000 else None


def extract_offers(m: dict[str, Any], url: str, text: str) -> list[dict[str, Any]]:
    """LLM 从一页正文里抽多条报价；抽不出返回 []。"""
    try:
        data = llm.chat_json([{"role": "system", "content": "你是采购信息抽取员，输出严格 JSON。"},
                              {"role": "user", "content": EXTRACT_PROMPT.format(name=m["name"], key=keyword(m), url=url, text=text[:8000])}],
                             max_tokens=4000)
    except Exception as e:  # noqa: BLE001
        _log(f"抽取失败 {url[:60]}：{e}")
        return []
    out = []
    for o in (data.get("offers") if isinstance(data, dict) else None) or []:
        price = _to_price(o.get("price_rmb_per_ton"))
        name = _norm_name(o.get("supplier") or "")
        if not price or not name:
            continue
        kind = o.get("kind") if o.get("kind") in KINDS else "未知"
        if not _valid_supplier(name, kind):
            continue
        out.append({"supplier": name, "kind": kind, "region": (o.get("region") or "")[:40], "grade": (o.get("grade") or "")[:60],
                    "price": price, "basis": _clean_basis(o.get("basis")), "moq": _clean_basis(o.get("moq"))[:40],
                    "date": (o.get("date") or "")[:10], "contact": (o.get("contact") or "")[:80],
                    "evidence": (o.get("evidence") or "")[:120]})
    return out


def upsert_supplier(data: dict[str, Any]) -> int:
    """按名称合并；已有记录只补空字段。"""
    name = _norm_name(data.get("name") or "")
    if not name:
        raise ValueError("供应商名称不能为空")
    row = db.q1("SELECT * FROM suppliers WHERE name=?", (name,))
    fields = {k: data.get(k) for k in ("kind", "region", "contact", "url", "materials", "notes", "credibility", "status") if data.get(k) not in (None, "")}
    if row:
        upd = {k: v for k, v in fields.items() if not row.get(k)}
        if data.get("materials") and row.get("materials"):
            codes = sorted(set(row["materials"].split(",")) | set(str(data["materials"]).split(",")))
            upd["materials"] = ",".join(c for c in codes if c)
        if upd:
            upd["updated_at"] = db.now()
            db.update("suppliers", row["id"], upd)
        return row["id"]
    return db.insert("suppliers", {"name": name, "kind": fields.get("kind", "未知"), "region": fields.get("region", ""),
                                   "contact": fields.get("contact", ""), "url": fields.get("url", ""),
                                   "materials": fields.get("materials", ""), "notes": fields.get("notes", ""),
                                   "credibility": fields.get("credibility", 3), "status": fields.get("status", "网查"),
                                   "created_at": db.now(), "updated_at": db.now()})


def add_quote(code: str, offer: dict[str, Any], source_url: str, credibility: int) -> int | None:
    sid = upsert_supplier({"name": offer["supplier"], "kind": offer.get("kind"), "region": offer.get("region"),
                           "contact": offer.get("contact"), "url": source_url, "materials": code, "credibility": credibility})
    # 同一厂商、同一材料、同一价、同一牌号只留一条（同一报价常出现在多个页面）
    dup = db.q1("SELECT id FROM supplier_quotes WHERE supplier_id=? AND material_code=? AND price=? AND IFNULL(grade,'')=?",
                (sid, code, offer["price"], offer.get("grade", "") or ""))
    if dup:
        return None
    return db.insert("supplier_quotes", {
        "supplier_id": sid, "material_code": code, "grade": offer.get("grade", ""), "price": offer["price"], "unit": "元/吨",
        "basis": offer.get("basis", ""), "moq": offer.get("moq", ""), "quote_date": offer.get("date", ""),
        "source_url": source_url, "evidence": offer.get("evidence", ""), "credibility": credibility, "status": "网查",
        "in_rfq": 0, "actual_price": None, "note": "", "created_at": db.now()})


# ---------------- 主流程 ----------------

def source_material(m: dict[str, Any], *, depth: str = "标准", extra: str = "", pages_per_query: int = 3,
                    echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    """一种材料：检索 → 抓页 → 抽取 → 入库。返回 {code, n_offers, n_new, urls}。"""
    code = m["code"]
    seen_urls: set[str] = set()
    pages: list[tuple[str, str]] = []
    for q in queries_for(m, depth, extra):
        try:
            hits = web_search(q, limit=8, bing_first=q.startswith("site:"))
        except Exception as e:  # noqa: BLE001
            _log(f"[{code}] 搜索失败「{q}」：{str(e)[:80]}", echo)
            continue
        picked = 0
        for h in hits:
            if picked >= pages_per_query:
                break
            if not h.url or h.url in seen_urls:
                continue
            seen_urls.add(h.url)
            try:
                page = fetch_url(h.url)
            except Exception:  # noqa: BLE001
                continue
            text = page.get("text") or ""
            if len(text) < 200:
                continue
            picked += 1
            pages.append((h.url, text))
    _log(f"[{code}] 检索完成：{len(pages)} 个有正文的页面，开始抽取报价", echo)
    n_offers = n_new = 0
    # 抽取是最慢的一步（每页一次模型调用，几十秒），3 个并行
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda p: (p[0], extract_offers(m, p[0], p[1])), pages))
    for url, offers in results:
        cred = _credibility(url)
        for o in offers:
            n_offers += 1
            if add_quote(code, o, url, cred) is not None:
                n_new += 1
        if offers:
            brief = ", ".join(o["supplier"][:12] + " " + f"{o['price']:.0f}" for o in offers[:3])
            _log(f"[{code}] {url[:60]} → {len(offers)} 条报价（{brief}）", echo)
    _log(f"[{code}] 完成：{n_offers} 条报价，新增 {n_new} 条，看了 {len(seen_urls)} 个页面", echo)
    return {"code": code, "n_offers": n_offers, "n_new": n_new, "urls": len(seen_urls)}


def run(codes: list[str] | None = None, *, depth: str = "标准", extra: str = "", only_producers: bool = False,
        echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    """对若干材料寻源；写 sourcing_runs 与报告。后台任务用 jobs.start('sourcing', ...) 包起来。"""
    mats = [m for m in MAT.list_materials(active_only=True) if not codes or m["code"] in codes]
    _log(f"开始寻源：{', '.join(m['code'] for m in mats)}（深度 {depth}）", echo)
    activity.note(f"供应商寻源：{len(mats)} 种材料")
    results = []
    for m in mats:
        results.append(source_material(m, depth=depth, extra=extra, echo=echo))
    summary = {r["code"]: {**r, **_compare_summary(r["code"], only_producers)} for r in results}
    db.insert("sourcing_runs", {"at": db.now(), "codes_json": db.dumps([m["code"] for m in mats]), "summary_json": db.dumps(summary),
                                "note": f"depth={depth}"})
    write_report(summary)
    _log(f"寻源结束：{sum(r['n_new'] for r in results)} 条新报价", echo)
    return summary


# ---------------- 比较 ----------------

def quotes(code: str | None = None, *, include_untrusted: bool = False) -> list[dict[str, Any]]:
    sql = ("SELECT q.*, s.name AS supplier, s.kind, s.region, s.contact, s.credibility AS supplier_cred "
           "FROM supplier_quotes q JOIN suppliers s ON s.id=q.supplier_id")
    conds, params = [], []
    if code:
        conds.append("q.material_code=?")
        params.append(code)
    if not include_untrusted:
        conds.append("q.status!='不可信'")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY q.price ASC, q.credibility DESC, q.id DESC"
    return db.q(sql, tuple(params))


def compare(code: str, *, only_producers: bool = False) -> list[dict[str, Any]]:
    """该材料的报价与现价比较（实价优先，否则估计价）。"""
    prices = MAT.current_prices().get(code, {})
    current = prices.get("actual") if prices.get("actual") is not None else prices.get("estimate")
    rows = []
    for q in quotes(code):
        if only_producers and q["kind"] not in ("生产商", "回收厂"):
            continue
        diff = (q["price"] - current) if current else None
        rows.append({**q, "current": current, "diff": diff, "diff_pct": (diff / current * 100) if current else None,
                     "basis_flag": bool(re.search(r"不含税|不含运|期货|平台标价", q.get("basis") or ""))})
    return rows


def _compare_summary(code: str, only_producers: bool = False) -> dict[str, Any]:
    rows = [r for r in compare(code, only_producers=only_producers) if r["kind"] != "行情"]
    market = [r for r in compare(code) if r["kind"] == "行情"]
    if not rows and not market:
        return {"n_suppliers": 0}
    best = rows[0] if rows else None
    return {
        "n_suppliers": len({r["supplier_id"] for r in rows}),
        "current": (rows or market)[0]["current"],
        "min_price": best["price"] if best else None,
        "best_supplier": best["supplier"] if best else None,
        "saving_pct": round(-best["diff_pct"], 1) if best and best["diff_pct"] is not None else None,
        "market": (f"{market[0]['supplier']} {market[0]['price']:.0f}" if market else ""),
    }


def summary_text(code: str) -> str:
    s = _compare_summary(code)
    m = MAT.get_material(code) or {"name": code}
    if not s.get("n_suppliers") and not s.get("market"):
        return f"{m['name']}：尚无报价，先运行寻源。"
    parts = [f"{m['name']}：{s['n_suppliers']} 家有报价"]
    if s.get("min_price"):
        parts.append(f"最低 {s['min_price']:.0f} 元/吨（{s['best_supplier']}）")
    if s.get("current"):
        parts.append(f"现价 {s['current']:.0f}")
        if s.get("saving_pct") is not None:
            parts.append(("低 " if s["saving_pct"] > 0 else "高 ") + f"{abs(s['saving_pct']):.1f}%")
    if s.get("market"):
        parts.append(f"行情 {s['market']}")
    return "，".join(parts) + "。网查价需询价核实。"


# ---------------- 询价清单与登记 ----------------

def set_quote(quote_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("in_rfq", "status", "actual_price", "note")}
    if allowed:
        db.update("supplier_quotes", quote_id, allowed)


def rfq_rows() -> list[dict[str, Any]]:
    return [q for q in quotes(include_untrusted=False) if q.get("in_rfq")]


def export_rfq(path: Path | None = None) -> Path:
    path = path or RFQ_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    names = {m["code"]: m["name"] for m in MAT.list_materials(active_only=False)}
    rows = [{"材料": names.get(q["material_code"], q["material_code"]), "厂商": q["supplier"], "类型": q["kind"], "地区": q["region"],
             "牌号/规格": q["grade"], "网查报价(元/吨)": q["price"], "口径": q["basis"], "起订量": q["moq"], "报价日期": q["quote_date"],
             "联系方式": q["contact"], "来源": q["source_url"], "要确认": "含税？到厂？起订量？账期？供样？",
             "实际报价(元/吨)": q.get("actual_price"), "状态": q["status"], "备注": q.get("note", "")} for q in rfq_rows()]
    df = pd.DataFrame(rows or [{"材料": "", "厂商": "（询价清单为空：在“结果”里勾选加入）"}])
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="询价清单", index=False)
    return path


def register_actual(quote_id: int, actual_price: float, note: str = "") -> dict[str, Any]:
    """把询价得到的实际报价登记为该材料的实价（联动推荐），并标记该条报价为已登记。"""
    q = db.q1("SELECT q.*, s.name AS supplier FROM supplier_quotes q JOIN suppliers s ON s.id=q.supplier_id WHERE q.id=?", (quote_id,))
    if not q:
        raise ValueError("报价不存在")
    res = pricing.record_price(q["material_code"], float(actual_price), "actual", source=f"询价：{q['supplier']}",
                               supplier=q["supplier"], url=q.get("source_url") or "", moq=q.get("moq") or "",
                               note=note or f"供应商寻源 → 询价核实（网查 {q['price']:.0f}）")
    set_quote(quote_id, status="已登记", actual_price=float(actual_price), note=note)
    db.update("suppliers", q["supplier_id"], {"status": "已报价", "updated_at": db.now()})
    return res


# ---------------- 厂商库 ----------------

def list_suppliers() -> list[dict[str, Any]]:
    rows = db.q("SELECT s.*, COUNT(q.id) AS n_quotes, MIN(q.price) AS min_price FROM suppliers s "
                "LEFT JOIN supplier_quotes q ON q.supplier_id=s.id AND q.status!='不可信' GROUP BY s.id ORDER BY s.name")
    return rows


def delete_supplier(supplier_id: int) -> None:
    db.delete("suppliers", supplier_id)


# ---------------- 报告 ----------------

def write_report(summary: dict[str, Any]) -> Path:
    names = {m["code"]: m["name"] for m in MAT.list_materials(active_only=False)}
    lines = [f"# 供应商寻源报告（{db.now()[:16].replace('T', ' ')}）", "",
             "网查到的是挂牌价 / 平台标价，口径常不一致；用于确定该向谁询价与大致区间，实价以询价为准。", ""]
    for code, s in summary.items():
        lines.append(f"## {names.get(code, code)}（{code}）")
        lines.append(f"- {summary_text(code)}")
        top = [r for r in compare(code) if r["kind"] != "行情"][:8]
        if top:
            lines.append("")
            lines.append("| 厂商 | 类型 | 地区 | 牌号 | 报价 | 口径 | 日期 | 与现价 | 来源 |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for r in top:
                d = f"{r['diff']:+.0f}（{r['diff_pct']:+.1f}%）" if r.get("diff") is not None else "-"
                lines.append(f"| {r['supplier']} | {r['kind']} | {r['region']} | {r['grade']} | {r['price']:.0f} | {r['basis']} | {r['quote_date']} | {d} | {r['source_url'][:50]} |")
        lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH


def last_run() -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM sourcing_runs ORDER BY id DESC LIMIT 1")
    if r:
        r["summary"] = db.loads(r.get("summary_json"), {})
        r["codes"] = db.loads(r.get("codes_json"), [])
    return r
