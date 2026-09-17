"""后台任务：在服务器进程内用线程运行流水线阶段，界面轮询日志与状态。"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable

from . import activity
from .pipeline import runner, state

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def is_running(name: str | None = None) -> bool:
    with _lock:
        if name:
            j = _jobs.get(name)
            return bool(j and j["thread"].is_alive())
        return any(j["thread"].is_alive() for j in _jobs.values())


def current() -> str | None:
    with _lock:
        for k, j in _jobs.items():
            if j["thread"].is_alive():
                return k
    return None


def info() -> dict[str, Any] | None:
    """正在运行的任务：名称、开始时间、已运行秒数；没有则 None。"""
    with _lock:
        for k, j in _jobs.items():
            if j["thread"].is_alive():
                return {"name": k, "started": j["started"], "elapsed": time.time() - j["started"]}
    return None


def result(name: str) -> dict[str, Any] | None:
    with _lock:
        j = _jobs.get(name)
        return None if not j else {"done": not j["thread"].is_alive(), "error": j.get("error"), "result": j.get("result")}


def _label(name: str) -> str:
    return "研发流水线" if name == "discovery" else state.STAGE_NAMES.get(name, name)


def start(name: str, fn: Callable[[], Any]) -> bool:
    """启动任务；已有任务在跑时返回 False。"""
    with _lock:
        if any(j["thread"].is_alive() for j in _jobs.values()):
            return False
        job: dict[str, Any] = {"error": None, "result": None, "started": time.time()}

        def _run():
            activity.note(f"开始 {_label(name)}")
            try:
                job["result"] = fn()
                activity.note(f"完成 {_label(name)}（{activity.fmt_seconds(time.time() - job['started'])}）")
            except Exception as e:  # noqa: BLE001
                job["error"] = f"{type(e).__name__}: {e}"
                activity.note(f"失败 {_label(name)}：{type(e).__name__}", ok=False)
                state.log(f"任务 {name} 异常：{e}\n{traceback.format_exc()[-800:]}")

        t = threading.Thread(target=_run, name=f"polysage-{name}", daemon=True)
        job["thread"] = t
        _jobs[name] = job
        t.start()
        return True


def start_discovery(stages: list[str] | None = None, resume: bool = True, **kw: Any) -> bool:
    return start("discovery", lambda: runner.run_discovery(stages, echo=None, resume=resume, **kw))


def start_stage(key: str, **kw: Any) -> bool:
    return start(key, lambda: runner.run_stage(key, echo=None, **kw))
