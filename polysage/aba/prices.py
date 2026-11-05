"""价格场景：当前价、按期货修正到今天、近月/次近月期货、自定义涨跌。

国内只有 LLDPE 有期货（大商所 L）。其他料按与 LLDPE 的联动程度跟着走：
新料 PE（LDPE、HDPE、茂金属、弹性体）联动 1.0，再生料 0.6，填充料和助剂不跟（0）。
“按期货修正到今天”= 价格卡上 LLDPE 的报价日 → 今天，L 主连涨跌了多少，就按这个比例把价格卡推到今天。
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from .. import db
from . import rules as R
from .model import current_price_map

SCENARIOS = {"current": "当前价（价格卡）", "live": "按期货修正到今天", "fut1": "近月期货", "fut2": "次近月期货",
             "custom": "自定义涨跌"}
ELASTICITY = {"lldpe": 1.0, "lldpe_c6": 1.0, "mlldpe": 1.0, "ldpe": 1.0, "hdpe": 1.0, "mdpe": 1.0, "elastomer": 1.0,
              "recycled_ld": 0.6, "recycled_ll": 0.6, "pcr": 0.6}
STALE_DAYS = 14
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, fn, ttl: float = 600.0):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.monotonic(), val)
    return val


def price_row(code: str) -> dict[str, Any] | None:
    """价格卡上这种料当前生效的那条（实价优先，其次估计价）。"""
    for tier in ("actual", "estimate"):
        r = db.q1("SELECT price, price_date, source, price_type FROM prices WHERE material_code=? AND price_type=? "
                  "ORDER BY id DESC LIMIT 1", (code, tier))
        if r:
            return r
    return None


def _as_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s)
    for text, fmt in ((s[:10], "%Y-%m-%d"), (s[:7], "%Y-%m")):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _kline(symbol: str, days: int) -> list[dict[str, Any]]:
    from ..forecast import fetch_kline

    return _cached(f"k:{symbol}:{days}", lambda: fetch_kline(symbol, days))


def _close_on(kl: list[dict[str, Any]], d: date) -> float | None:
    best = None
    for k in kl:
        kd = _as_date(k["d"])
        if kd and kd <= d:
            best = k["close"]
    return best


def build(name: str = "current", custom_pct: float = 0.0, *, roles: dict[str, str] | None = None) -> dict[str, Any]:
    """返回 {name, label, prices: {代码: 元/吨}, ratio: LLDPE 倍数, notes: [...], ref: {...}}。"""
    from ..forecast import next_contracts
    from ..formulation.materials import list_materials

    base = current_price_map()
    roles = roles or {m["code"]: R.classify(m) for m in list_materials(active_only=False)}
    notes: list[str] = []
    ref: dict[str, Any] = {}
    row = price_row("LL")
    ref_date = _as_date(row["price_date"]) if row else None
    if row:
        ref.update({"LL": row["price"], "LL_date": row["price_date"], "LL_source": (row["source"] or "")[:40]})
    if ref_date and (date.today() - ref_date).days > STALE_DAYS:
        notes.append(f"价格卡上 LLDPE 的报价停在 {ref_date}（{(date.today() - ref_date).days} 天前），建议更新贸易商日报，"
                     "或用“按期货修正到今天”看现在大概多少。")
    ratio = 1.0
    if name in ("live", "fut1", "fut2"):
        try:
            kl = _kline("L0", 250)
            if not kl:
                raise RuntimeError("期货行情为空")
            l_now = kl[-1]["close"]
            l_then = _close_on(kl, ref_date) if ref_date else None
            ratio = (l_now / l_then) if l_then else 1.0
            ref.update({"L_now": l_now, "L_now_date": kl[-1]["d"], "L_then": l_then})
            if not l_then:
                notes.append("找不到价格卡报价日的期货收盘价，按今天的价格卡计（不修正）。")
            if name in ("fut1", "fut2"):
                syms = next_contracts(n=2)
                sym = syms[0 if name == "fut1" else 1]
                fk = _kline(sym, 5)
                if not fk:
                    raise RuntimeError(f"{sym} 无行情")
                ratio *= fk[-1]["close"] / l_now
                ref.update({"contract": sym, "F": fk[-1]["close"]})
                if abs(fk[-1]["close"] / l_now - 1) < 0.001:
                    notes.append(f"{sym} 现在就是主力合约，和“按期货修正到今天”一样；想看远一点选次近月。")
        except Exception as e:  # noqa: BLE001
            notes.append(f"期货数据取不到（{str(e)[:60]}），这次按当前价格卡计算。")
            ratio = 1.0
            name = "current"
    elif name == "custom":
        ratio = 1 + float(custom_pct) / 100
    prices = {}
    for code, p in base.items():
        e = ELASTICITY.get(roles.get(code, ""), 0.0)
        prices[code] = round(p * (1 + e * (ratio - 1)), 1)
    label = SCENARIOS.get(name, name)
    if name == "custom":
        label = f"自定义：LLDPE {custom_pct:+g}%"
    elif name in ("fut1", "fut2") and ref.get("contract"):
        label = f"{SCENARIOS[name]}（{ref['contract']}）"
    if ratio != 1.0:
        notes.append(f"LLDPE 按 ×{ratio:.3f} 计；新料 PE 同步、再生料按 0.6 跟随、填充料与助剂不变。")
    return {"name": name, "label": label, "prices": prices, "ratio": round(ratio, 4), "notes": notes, "ref": ref}
