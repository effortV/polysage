"""价格联动：价格是唯一成本来源。任何价格变更 → 全部配方成本重算 → 重排 → 变化报告。

- record_price(): 登记价格（估计 / 实价）并触发联动。
- refresh_estimates(): 逐个材料网查参考价（需 LLM 抽取），偏差 > 40% 的记为待核对不自动采用。
- auto_refresh_if_due(): 每周自动刷新（由界面/命令行的调度线程调用）。
- 变化报告写到 04_价格卡/价格变动报告.md，并记录 price_events。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import db, kb
from .config import DATA_DIR, KB, settings
from .formulation import constraints as C
from .formulation import cost as COST
from .formulation import library as L
from .formulation import materials as MAT

REPORT_PATH = KB["price"] / "价格变动报告.md"
REFRESH_STATE = DATA_DIR / "price_refresh.json"
RANK_STATUSES = ("候选", "推荐", "试验中")


def snapshot() -> dict[str, dict[str, Any]]:
    """当前配方库中参与排名的配方：code → {cost, rank}。"""
    rows = []
    for f in L.list_formulations():
        if f["status"] not in RANK_STATUSES:
            continue
        cost = f.get("cost_used")
        if cost is None:
            continue
        rows.append((f["code"], float(cost)))
    rows.sort(key=lambda x: x[1])
    return {code: {"cost": cost, "rank": i + 1} for i, (code, cost) in enumerate(rows)}


def on_price_change(trigger: str, changed: list[dict[str, Any]] | None = None, before: dict[str, dict[str, Any]] | None = None,
                    cost_threshold: float = 100.0, rank_threshold: int = 3) -> dict[str, Any]:
    """重算全部配方成本、对比变动前快照、写报告。返回摘要。"""
    before = before if before is not None else {}
    L.recompute_costs()
    after = snapshot()
    c = C.load()
    base_cost = COST.compute_simple(C.base_formulation(c))
    moves = []
    for code, a in after.items():
        b = before.get(code)
        if not b:
            moves.append({"code": code, "cost_before": None, "cost_after": a["cost"], "rank_before": None, "rank_after": a["rank"], "flag": "新增"})
            continue
        d_cost = a["cost"] - b["cost"]
        d_rank = a["rank"] - b["rank"]
        flag = ""
        if abs(d_cost) >= cost_threshold:
            flag += f"成本变动 {d_cost:+.0f}；"
        if abs(d_rank) >= rank_threshold:
            flag += f"排名变动 {d_rank:+d}；"
        moves.append({"code": code, "cost_before": b["cost"], "cost_after": a["cost"], "rank_before": b["rank"], "rank_after": a["rank"], "flag": flag})
    flagged = [m for m in moves if m["flag"]]
    summary = {"at": db.now(), "trigger": trigger, "changed_prices": changed or [], "base_cost": base_cost,
               "cost_limit": c.get("cost_limit"), "n_formulations": len(after), "n_flagged": len(flagged),
               "top5": [{"code": k, "cost": v["cost"]} for k, v in sorted(after.items(), key=lambda kv: kv[1]["rank"])[:5]]}
    _write_report(summary, moves)
    db.insert("price_events", {"at": summary["at"], "trigger": trigger, "summary_json": db.dumps({**summary, "moves": moves}),
                               "report_path": str(REPORT_PATH)})
    return {**summary, "moves": moves}


def cost_text(v: float | None) -> str:
    """成本可能算不出来（base 里某种料还没有价）：写清楚，别让格式化炸掉。"""
    return f"{v:.0f}" if isinstance(v, (int, float)) else "未知（base 里有料还没有价）"


def _write_report(summary: dict[str, Any], moves: list[dict[str, Any]]) -> None:
    lines = [f"# 价格变动报告（{summary['at']}）", "", f"触发：{summary['trigger']}", ""]
    if summary.get("changed_prices"):
        lines.append("## 本次价格变更")
        for ch in summary["changed_prices"]:
            lines.append(f"- {ch.get('code')}：{ch.get('old')} → {ch.get('new')} 元/吨（{ch.get('type')}，{ch.get('source', '')}）")
        lines.append("")
    lines += [f"base 成本：{cost_text(summary['base_cost'])} 元/吨；成本上限：{summary['cost_limit']}", "",
              "## 排名变化（标记：成本变动 ≥ 100 元/吨 或 排名变动 ≥ 3 位）", "",
              "| 配方 | 成本(前) | 成本(后) | 排名(前) | 排名(后) | 标记 |", "|---|---|---|---|---|---|"]
    for m in sorted(moves, key=lambda x: x["rank_after"]):
        lines.append(f"| {m['code']} | {m['cost_before'] if m['cost_before'] is None else round(m['cost_before'])} | {round(m['cost_after'])} | "
                     f"{m['rank_before'] or '-'} | {m['rank_after']} | {m['flag']} |")
    prev = db.q("SELECT at, trigger FROM price_events ORDER BY id DESC LIMIT 10")
    if prev:
        lines += ["", "## 最近的价格事件", *(f"- {p['at']} {p['trigger']}" for p in prev)]
    kb.write_text(REPORT_PATH, "\n".join(lines) + "\n")


def record_price(code: str, price: float, price_type: str = "estimate", **kw: Any) -> dict[str, Any]:
    """登记价格并联动（对外统一入口；界面/工具/回填都走这里）。"""
    before = snapshot()
    prices = MAT.current_prices().get(code, {})
    old = prices.get(price_type)
    MAT.add_price(code, price, price_type, **kw)
    changed = [{"code": code, "old": old, "new": price, "type": price_type, "source": kw.get("source", "")}]
    return on_price_change(f"登记价格 {code} {price_type}", changed, before)


def delete_price(price_id: int) -> dict[str, Any] | None:
    """删除一条价格记录并联动。"""
    row = db.q1("SELECT * FROM prices WHERE id=?", (price_id,))
    if not row:
        return None
    before = snapshot()
    db.delete("prices", price_id)
    MAT.export_price_card()
    res = on_price_change(f"撤销价格 {row['material_code']} {row['price_type']} {row['price']:.0f}",
                          [{"code": row["material_code"], "old": row["price"], "new": MAT.current_prices().get(row["material_code"], {}).get(row["price_type"]),
                            "type": row["price_type"], "source": "撤销"}], before)
    res["deleted"] = row
    return res


def undo_last_price(code: str, price_type: str | None = None) -> dict[str, Any] | None:
    if price_type:
        row = db.q1("SELECT id FROM prices WHERE material_code=? AND price_type=? ORDER BY id DESC", (code, price_type))
    else:
        row = db.q1("SELECT id FROM prices WHERE material_code=? AND price_type IN ('estimate','actual') ORDER BY id DESC", (code,))
    return delete_price(row["id"]) if row else None


def basis_summary() -> dict[str, Any]:
    """价格基准摘要：几项实价/估计价、最近更新时间。"""
    mats = MAT.list_materials(active_only=True)
    n_actual = sum(1 for m in mats if m.get("price_actual") is not None)
    n_est = sum(1 for m in mats if m.get("price_actual") is None and m.get("price_estimate") is not None)
    last = db.q1("SELECT created_at, material_code, price_type, price FROM prices ORDER BY id DESC LIMIT 1") or {}
    return {"n_actual": n_actual, "n_estimate": n_est, "n_missing": len(mats) - n_actual - n_est, "last": last}


def import_actual_prices(rows: list[dict[str, Any]]) -> dict[str, Any]:
    before = snapshot()
    old_prices = MAT.current_prices()
    n = MAT.import_actual_prices(rows)
    new_prices = MAT.current_prices()
    changed = [{"code": k, "old": old_prices.get(k, {}).get("actual"), "new": v.get("actual"), "type": "actual", "source": "甲方回填"}
               for k, v in new_prices.items() if v.get("actual") != old_prices.get(k, {}).get("actual")]
    res = on_price_change(f"甲方回填实价 {n} 条", changed, before)
    res["n_imported"] = n
    return res


def refresh_estimates(codes: list[str] | None = None, echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    """网查参考价刷新（需 LLM）。只刷新非“甲方口述/实价”材料；偏差>40% 记待核对。"""
    from .pipeline.stage_scout import QUERIES, estimate_price

    before = snapshot()
    old_prices = MAT.current_prices()
    changed = []
    mats = [m for m in MAT.list_materials(active_only=True) if not codes or m["code"] in codes]
    for m in mats:
        if m.get("price_actual") is not None:
            continue
        q = QUERIES.get(m["code"], (None, None, f"{m['name']} 价格"))[2]
        if not q:
            continue
        try:
            got = estimate_price(m["code"], m["name"], q, echo)
        except Exception as e:  # noqa: BLE001
            if echo:
                echo(f"刷新 {m['code']} 失败：{e}")
            continue
        if got and got.get("adopted"):
            changed.append({"code": m["code"], "old": old_prices.get(m["code"], {}).get("estimate"), "new": got["price"],
                            "type": "estimate", "source": got.get("url", "")})
    REFRESH_STATE.write_text(json.dumps({"last_run": db.now(), "n_changed": len(changed)}, ensure_ascii=False), encoding="utf-8")
    res = on_price_change(f"每周参考价刷新（{len(changed)} 项变化）", changed, before)
    res["n_checked"] = len(mats)
    return res


def last_refresh() -> str | None:
    if REFRESH_STATE.exists():
        try:
            return json.loads(REFRESH_STATE.read_text(encoding="utf-8")).get("last_run")
        except json.JSONDecodeError:
            return None
    return None


def auto_refresh_if_due(days: int = 7, echo: Callable[[str], None] | None = None) -> bool:
    if not settings.llm_ready:
        return False
    last = last_refresh()
    if last:
        from datetime import datetime

        if (datetime.now() - datetime.fromisoformat(last)).days < days:
            return False
    refresh_estimates(echo=echo)
    return True


_scheduler_started = False


def start_scheduler(interval_hours: float = 6.0, days: int | None = None) -> None:
    """后台线程：每隔 interval_hours 检查一次是否到期（到期即刷新）。界面启动时调用一次。

    默认关闭；在 .env 里设 PRICE_AUTO_REFRESH_DAYS=7 才启用（避免一启动就联网改价）。
    """
    import os

    global _scheduler_started
    if days is None:
        try:
            days = int(os.getenv("PRICE_AUTO_REFRESH_DAYS", "0") or 0)
        except ValueError:
            days = 0
    if _scheduler_started or days <= 0:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                auto_refresh_if_due(days=days)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval_hours * 3600)

    threading.Thread(target=_loop, name="polysage-price-scheduler", daemon=True).start()


def events(limit: int = 20) -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM price_events ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["summary"] = db.loads(r.get("summary_json"), {})
    return rows
