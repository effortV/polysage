"""侧栏「后台运行」面板的数据源：activity 计数与事件。"""
from __future__ import annotations

import pytest

from polysage import activity, jobs, ui


def test_track_counts_and_events():
    before = activity.snapshot()["counters"].get("llm", {}).get("total", 0)
    with activity.track("llm", "m"):
        pass
    with pytest.raises(ValueError):
        with activity.track("search", "api.openalex.org"):
            raise ValueError("boom")
    snap = activity.snapshot()
    assert snap["counters"]["llm"]["total"] == before + 1
    assert snap["counters"]["llm"]["inflight"] == 0
    assert snap["counters"]["search"]["errors"] >= 1
    assert snap["events"][0]["ok"] is False and snap["events"][0]["label"] == "api.openalex.org"
    assert not activity.busy()


def test_overview_and_html():
    activity.note("开始 研发流水线")
    ov = activity.overview()
    assert ov["current"] is None and jobs.info() is None
    assert any(ln["name"] == "模型调用" for ln in ov["lines"])
    html = ui._activity_html(ov)
    assert "后台运行" in html and "空闲" in html and "模型调用" in html
    assert activity.fmt_seconds(75) == "1 分 15 秒" and activity.fmt_seconds(3.14) == "3.1 s"
