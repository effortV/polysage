"""流水线状态（checkpoint）：data/pipeline_state.json，每个阶段的状态、耗时、产出文件、统计。"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from ..config import DATA_DIR
from ..db import now

STATE_PATH = DATA_DIR / "pipeline_state.json"
LOG_PATH = DATA_DIR / "pipeline.log"

STAGES = [
    ("collect", "① 资料采集"),
    ("mechanism", "② 现配方机理分析"),
    ("scout", "③ 替代材料发现"),
    ("design", "④ 候选配方与成本"),
    ("report", "⑤ 报告与首轮实验意见"),
    ("doe", "⑥ 首轮 DOE 方案"),
    ("learn", "⑦ 数据回灌与建模"),
    ("scan", "⑧ 扫描候选空间与推荐"),
    ("review", "⑨ 复盘"),
]
STAGE_NAMES = dict(STAGES)


def load() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"stages": {}, "updated_at": now()}


def save(state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def log(msg: str, echo: Callable[[str], None] | None = None) -> None:
    line = f"[{now()}] {msg}"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if echo:
        echo(line)


def set_stage(key: str, **fields: Any) -> dict[str, Any]:
    st = load()
    cur = st["stages"].get(key, {})
    cur.update(fields)
    st["stages"][key] = cur
    save(st)
    return cur


def stage(key: str) -> dict[str, Any]:
    return load()["stages"].get(key, {})


@contextmanager
def running(key: str, echo: Callable[[str], None] | None = None):
    """阶段执行上下文：记录开始/结束/异常与耗时。"""
    t0 = time.time()
    set_stage(key, status="running", started_at=now(), error=None)
    log(f"开始 {STAGE_NAMES.get(key, key)}", echo)
    try:
        yield
    except Exception as e:  # noqa: BLE001
        cancelled = type(e).__name__ == "JobCancelled"
        set_stage(key, status="cancelled" if cancelled else "failed",
                  error="已取消" if cancelled else f"{type(e).__name__}: {str(e)[:500]}", finished_at=now(),
                  seconds=round(time.time() - t0, 1))
        log(("已取消 " if cancelled else "失败 ") + STAGE_NAMES.get(key, key) + ("" if cancelled else f"：{e}"), echo)
        raise
    else:
        set_stage(key, status="done", finished_at=now(), seconds=round(time.time() - t0, 1))
        log(f"完成 {STAGE_NAMES.get(key, key)}（{time.time() - t0:.0f}s）", echo)


def read_log(n: int = 200) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(encoding="utf-8").splitlines()[-n:]
