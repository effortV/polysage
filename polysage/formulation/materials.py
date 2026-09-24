"""原料库与价格卡：现配方在用的 4 种料预置，其余由智能体检索发现（带来源）；价格分估计价 / 实价两档。"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .. import db
from ..config import KB

# 代码, 类别, 名称, 牌号示例, MFR, 密度, 共聚单体, 是否再生, 作用, 典型用量下限, 上限, 四项影响, 风险, 是否用于配方, 假设价
BASE_CODES = ("LL", "LD", "HD", "R1")      # 现配方在用的四种料，只有它们是预置的

# 原料库只预置现配方在用的这 4 种；其余候选料由智能体检索发现（带来源网址），不再照搬内部技术路线附录 D 的假设清单。
SEED_MATERIALS: list[dict[str, Any]] = [
    dict(code="LL", category="主体树脂", name="现用 LLDPE（牌号待确认）", grade="待甲方提供", mfi=2.0, density=0.918, comonomer="丁烯（待确认）", is_recycled=0,
         role="韧性骨架；现用主料，成本大头，替代对象", typical_min=0, typical_max=60, melting_point=122, vicat=100,
         effects={"拉伸": "基准", "撕裂": "基准", "穿刺": "基准", "热封": "基准"}, risk="现用；若为特殊牌号，替代实为降规格，需以其 TDS 为筛选基准",
         use_flag="是（基准）", origin="现配方在用", price=12000, price_note="甲方口述价（2026-09）"),
    dict(code="LD", category="主体树脂", name="LDPE 新料（2426H 类）", grade="2426H/2426K", mfi=2.0, density=0.922, comonomer="", is_recycled=0,
         role="加工性、膜泡稳定、光学、热封", typical_min=5, typical_max=20, melting_point=110,
         effects={"拉伸": "↓", "撕裂": "↓", "穿刺": "↓", "热封": "↑"}, risk="比 LLDPE 贵", use_flag="是（少量）",
         origin="现配方在用", price=None, price_note="价格只认贸易商日报 / 实际报价"),
    dict(code="HD", category="主体树脂", name="HDPE 膜料（5000S 类）", grade="5000S/HHMTR144", mfi=0.9, density=0.950, comonomer="", is_recycled=0,
         role="挺度（撕裂与外观代价大，默认 ≤5%）", typical_min=0, typical_max=10, melting_point=130,
         effects={"拉伸": "↑", "撕裂": "↓↓", "穿刺": "↓", "热封": "↓"}, risk="撕裂下降、发白", use_flag="减量/去除",
         origin="现配方在用", price=None, price_note="价格只认贸易商日报 / 实际报价"),
    dict(code="R1", category="再生料", name="再生高压一级透明料", grade="—", mfi=2.0, density=0.922, comonomer="", is_recycled=1,
         role="现用；成本主力（比例上限靠阶梯实验定）", typical_min=15, typical_max=60, melting_point=112,
         effects={"拉伸": "略降", "撕裂": "略降", "穿刺": "↓（凝胶）", "热封": "略降（杂质）"}, risk="批次波动；需进厂快检",
         use_flag="是", origin="现配方在用", price=8000, price_note="甲方口述价（2026-09）"),
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
    for r in rows:
        r["effects"] = db.loads(r.get("effects_json"), {})
        p = prices.get(r["code"], {})
        r["price_estimate"] = p.get("estimate")
        r["price_actual"] = p.get("actual")
        r["price"] = p.get("actual") if p.get("actual") is not None else p.get("estimate")
        r["price_tier"] = "实价" if p.get("actual") is not None else ("估计价" if p.get("estimate") is not None else "缺")
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
