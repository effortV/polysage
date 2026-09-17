"""HDU×恒诺 - 包装膜（AI4S）界面入口（包名 polysage）。启动：.\\run.ps1 或 streamlit run streamlit_app.py"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).parent / "assets"
st.set_page_config(page_title="HDU×恒诺 - 包装膜（AI4S）", page_icon=str(ASSETS / "hdu.png"), layout="wide")

from polysage import pricing, ui  # noqa: E402

pricing.start_scheduler()  # 参考价自动刷新（默认关闭，.env 设 PRICE_AUTO_REFRESH_DAYS 开启）
ui.inject_style()
try:
    # 以 UTF-8 读入后传字符串：st.logo 直接读文件时用系统默认编码，中文 Windows（GBK）会解码失败
    st.logo((ASSETS / "logo.svg").read_text(encoding="utf-8"), size="large")
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

ui.sidebar_activity()  # 侧栏底部：后台运行情况
page.run()
