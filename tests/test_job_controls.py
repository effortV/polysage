"""后台任务的暂停 / 继续 / 取消（侧栏「后台运行」两个按钮背后的逻辑）。"""
from __future__ import annotations

import threading
import time

from polysage import activity, jobs


def _wait(cond, timeout=5.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _job_with_calls(n: int, made: list[int], stop: threading.Event):
    def run():
        for i in range(n):
            if stop.is_set():
                return "stopped"
            with activity.track("llm", "fake"):   # 每次“模型调用”前过检查点
                made.append(i)
                time.sleep(0.05)
        return "done"
    return run


def test_pause_resume_and_cancel():
    made: list[int] = []
    stop = threading.Event()
    assert jobs.start("t-pause", _job_with_calls(200, made, stop))
    assert _wait(lambda: len(made) >= 3)
    jobs.pause()
    assert jobs.is_paused() and jobs.info()["paused"]
    time.sleep(0.4)
    n_paused = len(made)
    time.sleep(0.4)
    assert len(made) <= n_paused + 1          # 暂停后不再发起新的调用
    jobs.resume()
    assert _wait(lambda: len(made) > n_paused + 2)
    jobs.cancel()
    assert _wait(lambda: not jobs.is_running())
    assert jobs.result("t-pause")["error"] == "已取消"
    assert any("已取消" in e["label"] for e in activity.snapshot()["events"])
    activity.clear_events()
    assert activity.snapshot()["events"] == []


def test_checkpoint_noop_when_idle_and_new_job_clears_flags():
    assert not jobs.is_running()
    jobs.checkpoint()                          # 空闲时不阻塞不抛
    made: list[int] = []
    assert jobs.start("t-clean", _job_with_calls(3, made, threading.Event()))
    assert _wait(lambda: not jobs.is_running())
    assert jobs.result("t-clean")["error"] is None and made == [0, 1, 2]
