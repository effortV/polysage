"""原料库与价格卡：现配方在用的 4 种料预置，其余由智能体检索发现（带来源）；价格分估计价 / 实价两档。"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .. import db
from ..config import KB

BASE_CODES = ("LL", "R1", "FL", "HD")      # 现配方在用的料（ABA：基底 LLDPE + 高压再生，B 层填充，必要时 B 层加 HDPE）

# 原料库只预置现配方在用的料；其余候选料由智能体检索发现（带来源网址），不再照搬内部技术路线附录 D 的假设清单。
# effects 用新的三项指标写：纵向拉伸、横向拉伸、透光（定量系数在 polysage/aba/rules.py）。
SEED_MATERIALS: list[dict[str, Any]] = [
    dict(code="LL", category="主体树脂", name="现用 LLDPE（牌号待确认）", grade="待甲方提供", mfi=2.0, density=0.918, comonomer="丁烯（待确认）", is_recycled=0,
         role="A、B 两层的基底主料（现配方 50 kg / 75 kg）", typical_min=30, typical_max=100, melting_point=122, vicat=100,
         effects={"纵向拉伸": "基准", "横向拉伸": "基准", "透光": "基准"}, risk="若为特殊牌号，换料前以其 TDS 为准",
         use_flag="是（基准）", origin="现配方在用", price=12000, price_note="甲方口述价（2026-09）"),
    dict(code="R1", category="再生料", name="高压再生回料（近新料色）", grade="—", mfi=2.0, density=0.922, comonomer="", is_recycled=1,
         role="A、B 两层基底里的再生料（现配方 25 kg / 75 kg）", typical_min=0, typical_max=60, melting_point=112,
         effects={"纵向拉伸": "略降", "横向拉伸": "略降", "透光": "略降"}, risk="批次波动；需进厂快检",
         use_flag="是", origin="现配方在用", price=8250, price_note="甲方口述价（2026-11，8000~8500 取中值）"),
    dict(code="FL", category="填充母料", name="碳酸钙填充母料", grade="—", mfi=None, density=1.95, comonomer="", is_recycled=0,
         role="只加在 B 层（芯层），一般 10~50%，主要降本手段", typical_min=0, typical_max=50,
         effects={"纵向拉伸": "↓", "横向拉伸": "↓↓", "透光": "↓↓"}, risk="透光与拉伸下降；密度高，克重上升",
         use_flag="是（只进 B 层）", origin="现配方在用", price=2500, price_note="甲方口述价（2026-11）"),
    dict(code="HD", category="主体树脂", name="HDPE 膜料（低压）", grade="5000S 类", mfi=0.9, density=0.950, comonomer="", is_recycled=0,
         role="需要挺度/硬度时加在 B 层，约 10%", typical_min=0, typical_max=15, melting_point=130,
         effects={"纵向拉伸": "↑", "横向拉伸": "略降", "透光": "↓"}, risk="横向易劈裂、发白",
         use_flag="是（只进 B 层）", origin="现配方在用", price=None, price_note="价格只认贸易商日报 / 实际报价"),
    dict(code="LD", category="主体树脂", name="LDPE 新料（高压，2426H 类）", grade="2426H/2426K", mfi=2.0, density=0.922, comonomer="", is_recycled=0,
         role="A 层提光学、稳膜泡的候选", typical_min=0, typical_max=20, melting_point=110,
         effects={"纵向拉伸": "↓", "横向拉伸": "↓", "透光": "↑"}, risk="比 LLDPE 贵", use_flag="候选",
         origin="原料库预置", price=None, price_note="价格只认贸易商日报 / 实际报价"),
]

PRICE_NOTE = "甲方口述价，未见正式报价单"
ASSUMED_MARK = "内部技术路线附录 D"      # 早期虚构的假设价，清理时按它识别


NEW_FIELDS = ("melting_point", "vicat", "producer", "mwd", "proc_temp", "needs_ppa", "availability", "price_note")


def seed_materials(overwrite: bool = False) -> int:
    """写入种子材料与假设价；已存在的材料不覆盖（除非 overwrite），但会补齐新字段与甲方口述价。"""
    n = 0
    for m in SEED_MATERIALS:
        m = dict(m)
        price = m.pop("price")
        price_note = m.get("price_note") or ""
        existing = db.q1("SELECT * FROM materials WHERE code=?", (m["code"],))
        effects = m.pop("effects")
        data = {**m, "effects_json": db.dumps(effects)}
        if existing and not overwrite:
            # 旧库升级：只补空的新字段；去掉“透明”相关的旧标记
            fill = {k: data[k] for k in NEW_FIELDS if k in data and existing.get(k) in (None, "")}
            if "透明" in str(existing.get("use_flag") or "") or "非透明" in str(existing.get("role") or ""):
                fill.update({"use_flag": data["use_flag"], "role": data["role"], "risk": data["risk"], "name": data["name"]})
            eff_old = db.loads(existing.get("effects_json"), {}) or {}
            if "透明" in eff_old:
                eff_old.pop("透明", None)
                fill["effects_json"] = db.dumps(eff_old)
            if fill:
                db.update("materials", existing["id"], fill)
        elif existing:
            db.update("materials", existing["id"], data)
        else:
            db.insert("materials", data)
            n += 1
        if price is None:                      # 没有可追溯的价就不写，等日报 / 网查 / 询价给
            continue
        cur = db.q1("SELECT price, source FROM prices WHERE material_code=? AND price_type='estimate' ORDER BY id DESC", (m["code"],))
        if cur is None:
            db.insert("prices", {"material_code": m["code"], "price_type": "estimate", "price": price, "unit": "元/吨",
                                 "price_date": "2026-09", "source": price_note or PRICE_NOTE, "created_at": db.now()})
        elif price_note and "甲方口述" in price_note and (cur["price"] != price or "甲方口述" not in (cur["source"] or "")):
            db.insert("prices", {"material_code": m["code"], "price_type": "estimate", "price": price, "unit": "元/吨",
                                 "price_date": "2026-09", "source": price_note, "created_at": db.now()})
    export_price_card()
    return n


def evidence(code: str) -> dict[str, Any]:
    """这个料站得住吗：有没有贸易商日报 / 网查报价、非假设价、或智能体发现时留下的来源网址。"""
    quotes = db.q("SELECT COUNT(*) n FROM supplier_quotes WHERE material_code=?", (code,))[0]["n"]
    real = db.q("SELECT COUNT(*) n FROM prices WHERE material_code=? "
                "AND COALESCE(source,'') || COALESCE(note,'') NOT LIKE ?", (code, f"%{ASSUMED_MARK}%"))[0]["n"]
    m = db.q1("SELECT origin, source_url FROM materials WHERE code=?", (code,)) or {}
    found = bool(m.get("source_url")) or "智能体" in (m.get("origin") or "")
    return {"quotes": quotes, "prices": real, "found": found,
            "ok": bool(quotes or real or found or code in BASE_CODES)}


def purge_assumed(delete: bool = True) -> dict[str, Any]:
    """去掉早期照搬内部技术路线附录 D 的假设价；只靠假设价活着的候选料一并清掉。

    现配方在用的 4 种料（BASE_CODES）永远保留；已经被智能体查到报价或来源的料也保留。
    """
    like = f"%{ASSUMED_MARK}%"
    n_price = db.q("SELECT COUNT(*) n FROM prices WHERE COALESCE(source,'') || COALESCE(note,'') LIKE ?", (like,))[0]["n"]
    if delete and n_price:
        db.execute("DELETE FROM prices WHERE COALESCE(source,'') || COALESCE(note,'') LIKE ?", (like,))
    removed, kept = [], []
    for m in db.q("SELECT code, name FROM materials ORDER BY code"):
        code = m["code"]
        if code in BASE_CODES:
            continue
        if evidence(code)["ok"]:
            kept.append(code)
            continue
        removed.append(code)
        if delete:
            db.execute("DELETE FROM materials WHERE code=?", (code,))
    if delete:
        export_price_card()
    return {"prices_deleted": n_price, "removed": removed, "kept": kept}


def list_materials(active_only: bool = True) -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM materials" + (" WHERE active=1" if active_only else "") + " ORDER BY id")
    prices = current_prices()
    eff = effective_prices()
    for r in rows:
        r["effects"] = db.loads(r.get("effects_json"), {})
        p = prices.get(r["code"], {})
        r["price_estimate"] = p.get("estimate")
        r["price_actual"] = p.get("actual")
        e = eff.get(r["code"])
        r["price"] = e["price"] if e else None
        r["price_tier"] = ("缺" if not e else "实价" if e["type"] == "actual" else
                           ("网查价（实价过期）" if e["why"].startswith("网查价") else "估计价"))
    return rows


def get_material(code: str) -> dict[str, Any] | None:
    for m in list_materials(active_only=False):
        if m["code"] == code:
            return m
    return None


def upsert_material(data: dict[str, Any]) -> int:
    data = dict(data)
    if "effects" in data:
        data["effects_json"] = db.dumps(data.pop("effects"))
    existing = db.q1("SELECT id FROM materials WHERE code=?", (data["code"],))
    if existing:
        db.update("materials", existing["id"], data)
        return existing["id"]
    return db.insert("materials", data)


def current_prices() -> dict[str, dict[str, float]]:
    """每种材料最新的估计价与实价。"""
    out: dict[str, dict[str, float]] = {}
    for r in db.q("SELECT material_code, price_type, price FROM prices WHERE price_type IN ('estimate','actual') ORDER BY id"):
        out.setdefault(r["material_code"], {})[r["price_type"]] = float(r["price"])
    return out


STALE_ACTUAL_DAYS = 21      # 实价（日报/甲方报价）超过 3 周算过期
FRESH_ESTIMATE_DAYS = 14    # 能顶替过期实价的网查价：两周内的


def _as_date(s: Any):
    import datetime as _dt

    s = str(s or "")
    for text, fmt in ((s[:10], "%Y-%m-%d"), (s[:7], "%Y-%m")):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def effective_prices() -> dict[str, dict[str, Any]]:
    """每种料实际按哪个价算：实价优先；但实价已过期（报价日超过 3 周）、又有两周内更新的网查/估计价时，用更新的那个。
    返回 {代码: {price, type, date, source, why}}。"""
    import datetime as _dt

    latest: dict[str, dict[str, dict[str, Any]]] = {}
    for r in db.q("SELECT id, material_code, price_type, price, price_date, source FROM prices "
                  "WHERE price_type IN ('estimate','actual') ORDER BY id"):
        latest.setdefault(r["material_code"], {})[r["price_type"]] = r
    today = _dt.date.today()
    out: dict[str, dict[str, Any]] = {}
    for code, d in latest.items():
        a, e = d.get("actual"), d.get("estimate")
        pick, why = (a, "实价") if a else (e, "估计价")
        if a and e:
            ad, ed = _as_date(a["price_date"]), _as_date(e["price_date"])
            if ad and ed and ed > ad and (today - ad).days > STALE_ACTUAL_DAYS and (today - ed).days <= FRESH_ESTIMATE_DAYS:
                pick, why = e, f"网查价（实价停在 {ad}，已过 {(today - ad).days} 天，改用 {ed} 的网查价）"
        out[code] = {"price": float(pick["price"]), "type": pick["price_type"], "date": pick["price_date"] or "",
                     "source": pick["source"] or "", "why": why}
    return out


def add_price(code: str, price: float, price_type: str = "estimate", *, source: str = "", url: str = "",
              price_date: str = "", supplier: str = "", moq: str = "", note: str = "") -> int:
    pid = db.insert("prices", {"material_code": code, "price_type": price_type, "price": float(price), "unit": "元/吨",
                               "price_date": price_date or db.now()[:10], "source": source, "url": url,
                               "supplier": supplier, "moq": moq, "note": note, "created_at": db.now()})
    export_price_card()
    return pid


def price_history(code: str) -> list[dict[str, Any]]:
    return db.q("SELECT * FROM prices WHERE material_code=? ORDER BY id DESC", (code,))


def export_price_card(path: Path | None = None) -> Path:
    """把价格卡镜像到 knowledge/04_价格卡/价格卡.csv。"""
    path = path or KB["price"] / "价格卡.csv"
    rows = list_materials(active_only=False)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "密度", "熔点", "估计价(元/吨)", "估计价来源", "实价(元/吨)", "当前采用", "价差%"])
        for r in rows:
            est, act = r.get("price_estimate"), r.get("price_actual")
            diff = f"{(act - est) / est * 100:.1f}" if est and act else ""
            src = (db.q1("SELECT source FROM prices WHERE material_code=? AND price_type='estimate' ORDER BY id DESC", (r["code"],)) or {}).get("source", "")
            w.writerow([r["code"], r["name"], r.get("density"), r.get("melting_point"), est, (src or "")[:60], act, r["price_tier"], diff])
    return path


def import_actual_prices(rows: list[dict[str, Any]]) -> int:
    """甲方回填：rows 含 代码/到厂含税价/供应商/起订量/报价日期。"""
    n = 0
    for r in rows:
        code = str(r.get("代码") or r.get("code") or "").strip()
        price = r.get("到厂含税价") or r.get("price") or r.get("实价")
        if not code or price in (None, ""):
            continue
        try:
            price = float(str(price).replace(",", ""))
        except ValueError:
            continue
        add_price(code, price, "actual", source="甲方回填", supplier=str(r.get("供应商") or ""),
                  moq=str(r.get("起订量") or ""), price_date=str(r.get("报价日期") or ""))
        n += 1
    return n


# ---------- 再生料专项数据 ----------

def add_recycled_batch(data: dict[str, Any]) -> int:
    """登记一批再生料的来源与进厂快检（MFR/密度/灰分/水分/DSC/凝胶/气味/价格）。"""
    row = {k: data.get(k) for k in ("material_code", "supplier", "grade", "source_film", "batch_no", "mfr", "density", "ash",
                                    "moisture", "dsc_note", "gel_count", "odor", "price", "received_at", "note")}
    row["created_at"] = db.now()
    rid = db.insert("recycled_batches", row)
    export_recycled_batches()
    return rid


def list_recycled_batches(code: str | None = None) -> list[dict[str, Any]]:
    if code:
        return db.q("SELECT * FROM recycled_batches WHERE material_code=? ORDER BY id DESC", (code,))
    return db.q("SELECT * FROM recycled_batches ORDER BY id DESC")


def export_recycled_batches(path: Path | None = None) -> Path:
    import pandas as pd

    path = path or KB["material"] / "再生料批次记录.csv"
    rows = list_recycled_batches()
    cols = ["material_code", "supplier", "grade", "source_film", "batch_no", "mfr", "density", "ash", "moisture", "dsc_note",
            "gel_count", "odor", "price", "received_at", "note"]
    pd.DataFrame(rows, columns=["id", *cols, "created_at"] if rows else cols).to_csv(path, index=False, encoding="utf-8-sig")
    return path
