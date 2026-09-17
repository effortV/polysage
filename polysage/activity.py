"""进程级运行情况：模型调用、资料检索、后台任务的计数与最近事件，供侧栏「后台运行」面板展示。

所有浏览器会话共用一个服务进程，所以这里的计数是整个服务的情况，不是某个页面的。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

_lock = threading.Lock()
_started_at = time.time()
_counters: dict[str, dict[str, Any]] = {}
_events: deque[dict[str, Any]] = deque(maxlen=40)

CATEGORY_NAMES = {"llm": "模型调用", "embed": "向量化", "rerank": "重排", "search": "资料检索", "job": "后台任务"}


def _counter(category: str) -> dict[str, Any]:
    return _counters.setdefault(category, {"total": 0, "errors": 0, "inflight": 0, "last_seconds": None,
                                           "last_at": None, "seconds_total": 0.0})


@contextmanager
def track(category: str, label: str = "") -> Iterator[None]:
    """包住一次外部调用：记录进行中数量、耗时、成功/失败。"""
    t0 = time.time()
    with _lock:
        _counter(category)["inflight"] += 1
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        dt = time.time() - t0
        with _lock:
            c = _counter(category)
            c["inflight"] -= 1
            c["total"] += 1
            c["seconds_total"] += dt
            c["last_seconds"] = dt
            c["last_at"] = time.time()
            if not ok:
                c["errors"] += 1
            _events.append({"at": time.time(), "category": category, "label": label, "seconds": dt, "ok": ok})


def note(text: str, category: str = "job", ok: bool = True) -> None:
    """记录一条不计时的事件（任务开始/结束、工具调用等）。"""
    with _lock:
        _events.append({"at": time.time(), "category": category, "label": text, "seconds": None, "ok": ok})


def busy() -> bool:
    with _lock:
        return any(v["inflight"] > 0 for v in _counters.values())


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "counters": {k: dict(v) for k, v in _counters.items()},
            "events": list(_events)[::-1],
            "uptime": time.time() - _started_at,
        }


def fmt_seconds(s: float | None) -> str:
    if s is None:
        return "-"
    if s < 60:
        return f"{s:.1f} s" if s < 10 else f"{s:.0f} s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m} 分 {sec:02d} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m:02d} 分"


def fmt_clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"


def overview() -> dict[str, Any]:
    """汇总成侧栏要显示的结构：当前任务、各类调用计数、参考价刷新、最近事件。"""
    from . import jobs, pricing
    from .pipeline import state

    snap = snapshot()
    counters = snap["counters"]
    job = jobs.info()
    current: dict[str, Any] | None = None
    if job:
        stage_label = state.STAGE_NAMES.get(job["name"], "")
        if job["name"] == "discovery":
            running = [n for k, n in state.STAGES if state.stage(k).get("status") == "running"]
            stage_label = running[0] if running else "研发流水线"
        current = {"name": job["name"], "label": stage_label or job["name"], "elapsed": job["elapsed"]}
    lines: list[dict[str, Any]] = []
    for cat in ("llm", "search", "embed", "rerank"):
        c = counters.get(cat)
        if not c or (c["total"] == 0 and c["inflight"] == 0):
            continue
        lines.append({"name": CATEGORY_NAMES[cat], "total": c["total"], "inflight": c["inflight"],
                      "errors": c["errors"], "last_seconds": c["last_seconds"]})
    import os

    try:
        auto_days = int(os.getenv("PRICE_AUTO_REFRESH_DAYS", "0") or 0)
    except ValueError:
        auto_days = 0
    last = pricing.last_refresh()
    price = {"auto_days": auto_days, "last": last[:16].replace("T", " ") if last else None}
    return {"current": current, "busy": bool(job) or busy(), "lines": lines, "price": price,
            "events": snap["events"][:8], "uptime": snap["uptime"]}
