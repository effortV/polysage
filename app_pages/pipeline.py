from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from polysage import jobs, ui, wordout
from polysage.config import settings
from polysage.pipeline import state, task

ui.page_header("研发流水线", "文献采集、机理分析、找新料、报告——一般由配方助手按需自动启动，这里看进度和产出，也可以手动跑。")

t = task.load()
with ui.expander("任务书"):
    st.text(task.brief(t))
if not settings.llm_ready:
    st.caption("对话模型未配置：检索、入库、成本、DOE 可运行；筛选、机理报告、材料卡、定性评估会跳过。")

st.subheader("发现阶段")
c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    max_hits = st.number_input("每条检索式取多少条", 3, 50, 12)
with c2:
    resume = st.toggle("跳过已完成的阶段", value=True)
with c3:
    st.caption("完整一轮约 30～90 分钟，中断可续跑；命令行：python -m polysage.pipeline run")

busy = jobs.is_running()
if st.button("运行发现阶段", type="primary", disabled=busy):
    if jobs.start_discovery(resume=resume, max_hits_per_query=int(max_hits)):
        st.rerun()

st.markdown("##### 单步运行")
cols = ui.hrow(6)
for col, (key, name) in zip(cols, state.STAGES[:6]):
    if col.button(name, key=f"run_{key}", disabled=busy):
        if key in ("collect",):
            jobs.start_stage(key, max_hits_per_query=int(max_hits))
        else:
            jobs.start_stage(key)
        st.rerun()

st.subheader("实验闭环")
with st.container(border=True):
    st.caption("ABA 试验的方案表下载、实测回填和预测对比在「实验与模型」页；这里可以把后面三步当流水线阶段跑。")
    r0, r1, r2, r3 = ui.hrow(4)
    round_no = r0.number_input("轮次", 1, 20, 1)
    if r1.button("⑦ 用实测校准", disabled=busy):
        jobs.start_stage("learn")
        st.rerun()
    if r2.button("⑧ 推荐下一轮", disabled=busy):
        jobs.start_stage("scan")
        st.rerun()
    if r3.button("⑨ 复盘", disabled=busy):
        jobs.start_stage("review", round_no=int(round_no))
        st.rerun()


@st.fragment(run_every="3s" if jobs.is_running() else None)
def status_panel():
    st_ = state.load()
    cur = jobs.current()
    if cur:
        st.markdown(ui.running_label(f"正在运行：{cur}"))
    rows = []
    for key, name in state.STAGES:
        s = st_["stages"].get(key, {})
        rows.append({"阶段": name, "状态": {"done": "完成", "running": "运行中", "failed": "失败"}.get(s.get("status"), "未运行"),
                     "耗时(s)": s.get("seconds"), "完成时间": s.get("finished_at"), "错误": s.get("error"),
                     "产出": ", ".join(Path(p).name for p in s.get("outputs", []))})
    st.dataframe(pd.DataFrame(rows), hide_index=True, **ui.WIDE)
    with st.expander("日志", expanded=bool(cur)):
        st.code("\n".join(state.read_log(60)) or "（空）", language="text")


st.subheader("状态")
status_panel()

st.subheader("产出文件")
outs: list[Path] = []
for key, _ in state.STAGES:
    for p in state.stage(key).get("outputs", []):
        if Path(p).exists() and Path(p) not in outs:
            outs.append(Path(p))
if not outs:
    st.caption("还没有产出文件。")
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
for p in outs:
    c_name, c_btn = st.columns([4, 1])
    c_name.write(f"{p.parent.name} / **{p.name}**")
    if p.suffix.lower() == ".md":                         # 报告类一律给 Word
        c_btn.download_button("下载 Word", data=wordout.md_to_docx(p.read_text(encoding="utf-8"), p.stem),
                              file_name=p.stem + ".docx", mime=DOCX_MIME, key=f"dl_{p}")
    else:
        c_btn.download_button("下载", data=p.read_bytes(), file_name=p.name, key=f"dl_{p}")
