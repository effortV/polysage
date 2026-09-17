"""膜方（PolySage）界面入口。启动：.\\run.ps1 或 streamlit run streamlit_app.py"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="膜方 · 包装膜配方降本", page_icon=":material/science:", layout="wide")

from polysage import pricing, ui  # noqa: E402
from polysage.config import settings  # noqa: E402

pricing.start_scheduler()  # 参考价自动刷新（默认关闭，.env 设 PRICE_AUTO_REFRESH_DAYS 开启）
ui.inject_style()
try:
    st.logo("assets/logo.svg", size="large")
except Exception:  # noqa: BLE001  旧版 Streamlit 无 st.logo 或参数不同
    pass

page = st.navigation(
    {
        "工作": [
            st.Page("app_pages/agent.py", title="配方推荐", icon=":material/auto_awesome:", default=True),
            st.Page("app_pages/pipeline.py", title="研发流水线", icon=":material/account_tree:"),
            st.Page("app_pages/chat.py", title="对话", icon=":material/forum:"),
        ],
        "数据": [
            st.Page("app_pages/knowledge.py", title="知识库", icon=":material/library_books:"),
            st.Page("app_pages/materials.py", title="原料与价格", icon=":material/inventory_2:"),
            st.Page("app_pages/experiments.py", title="实验与模型", icon=":material/analytics:"),
        ],
        "系统": [st.Page("app_pages/settings.py", title="设置", icon=":material/settings:")],
    }
)

_pb = pricing.basis_summary()
ui.sidebar_status([
    f"模型：{settings.chat_model}" if settings.llm_ready else "模型：未配置",
    f"价格：{_pb['n_actual']} 实价 / {_pb['n_estimate']} 估计价",
])
page.run()
