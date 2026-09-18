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
_paused = threading.Event()   # 置位 = 暂停
_cancel = threading.Event()   # 置位 = 取消


class JobCancelled(Exception):
    """后台任务被用户取消（在检查点抛出）。"""


def _in_ui_thread() -> bool:
    """Streamlit 页面线程有 ScriptRunContext；后台任务线程及其派生的工作线程没有。"""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx(suppress_warning=True) is not None
    except Exception:  # noqa: BLE001  非 Streamlit 环境（命令行）
        return False


def checkpoint() -> None:
    """安全点：每次外部调用（模型/检索）前调用。暂停时阻塞，取消时抛 JobCancelled；只作用于后台任务线程。"""
    if not (_paused.is_set() or _cancel.is_set()) or not is_running() or _in_ui_thread():
        return
    while _paused.is_set() and not _cancel.is_set():
        time.sleep(0.5)
    if _cancel.is_set():
        raise JobCancelled("任务已取消")


def pause() -> None:
    if is_running():
        _paused.set()
        activity.note("暂停 " + _label(current() or ""))


def resume() -> None:
    if _paused.is_set():
        _paused.clear()
        if is_running():
            activity.note("继续 " + _label(current() or ""))


def cancel() -> None:
    """取消当前任务：置取消位并解除暂停，任务在下一个检查点退出。"""
    if is_running():
        _cancel.set()
        _paused.clear()


def is_paused() -> bool:
    return _paused.is_set() and is_running()


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
                return {"name": k, "started": j["started"], "elapsed": time.time() - j["started"],
                        "paused": _paused.is_set(), "cancelling": _cancel.is_set()}
    return None


def result(name: str) -> dict[str, Any] | None:
    with _lock:
        j = _jobs.get(name)
        return None if not j else {"done": not j["thread"].is_alive(), "error": j.get("error"), "result": j.get("result")}


JOB_NAMES = {"discovery": "研发流水线", "sourcing": "供应商寻源"}


def _label(name: str) -> str:
    return JOB_NAMES.get(name) or state.STAGE_NAMES.get(name, name)


def start_sourcing(codes: list[str] | None = None, **kw: Any) -> bool:
    from . import sourcing

    return start("sourcing", lambda: sourcing.run(codes, **kw))


def start(name: str, fn: Callable[[], Any]) -> bool:
    """启动任务；已有任务在跑时返回 False。"""
    with _lock:
        if any(j["thread"].is_alive() for j in _jobs.values()):
            return False
        job: dict[str, Any] = {"error": None, "result": None, "started": time.time()}
        _paused.clear()
        _cancel.clear()

        def _run():
            activity.note(f"开始 {_label(name)}")
            try:
                job["result"] = fn()
                activity.note(f"完成 {_label(name)}（{activity.fmt_seconds(time.time() - job['started'])}）")
            except JobCancelled:
                job["error"] = "已取消"
                activity.note(f"已取消 {_label(name)}", ok=False)
                state.log(f"任务 {name} 已被用户取消")
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
