"""Streamlit 版本兼容层：让页面在 1.45（Anaconda 自带）到 1.6x 都能跑。

新版才有的参数（container(horizontal=)、expander/status(type=)、chat_input(submit_mode=)、width="stretch"）
在旧版会直接报 TypeError，这里统一判断后再传。
"""
from __future__ import annotations

import inspect
from typing import Any

import streamlit as st


def _has_param(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


HAS_HORIZONTAL = _has_param(st.container, "horizontal")
HAS_EXPANDER_TYPE = _has_param(st.expander, "type")
HAS_STATUS_TYPE = _has_param(st.status, "type")
HAS_SUBMIT_MODE = _has_param(st.chat_input, "submit_mode")


def _supports_str_width() -> bool:
    """dataframe(width="stretch") 只在 1.47+ 可用；旧版用 use_container_width。"""
    try:
        major, minor = (int(x) for x in st.__version__.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (1, 47)


# 传给 dataframe / data_editor 的“占满宽度”参数
WIDE: dict[str, Any] = {"width": "stretch"} if _supports_str_width() else {"use_container_width": True}


def hrow(n: int):
    """一排 n 个等宽格子（用 columns 实现，所有版本通用）。"""
    return st.columns(n)


def expander(label: str, *, expanded: bool = False, step: bool = False):
    if step and HAS_EXPANDER_TYPE:
        return st.expander(label, expanded=expanded, type="step")
    return st.expander(label, expanded=expanded)


def status(label: str, *, step: bool = False, state: str = "running", expanded: bool = False):
    kw: dict[str, Any] = {"state": state, "expanded": expanded}
    if step and HAS_STATUS_TYPE:
        kw["type"] = "step"
    return st.status(label, **kw)


def chat_input(placeholder: str, *, disable_while_running: bool = True):
    if HAS_SUBMIT_MODE and disable_while_running:
        return st.chat_input(placeholder, submit_mode="disable")
    return st.chat_input(placeholder)


def running_label(text: str) -> str:
    """新版支持 :shimmer[] 动效，旧版直接显示文字。"""
    return f"⏳ {text}"


# ---------------- 视觉样式（一次注入） ----------------

_STYLE = """
<style>
/* 隐藏 Streamlit 自带的菜单/页脚/部署按钮 */
#MainMenu, footer, .stAppDeployButton, [data-testid="stDecoration"] {display: none !important;}
header[data-testid="stHeader"] {background: transparent;}

/* 版面 */
.block-container {padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1380px;}
h1 {font-size: 1.55rem !important; font-weight: 600 !important; letter-spacing: 0.01em; margin-bottom: 0.4rem !important;}
h2 {font-size: 1.15rem !important; font-weight: 600 !important; margin-top: 1.2rem !important;}
h3 {font-size: 1.0rem !important; font-weight: 600 !important;}
p, li, .stMarkdown {line-height: 1.55;}
[data-testid="stCaptionContainer"] {color: #6B7280 !important;}

/* 侧栏 */
section[data-testid="stSidebar"] {background: #F3F5F7; border-right: 1px solid #E5E7EB;}
section[data-testid="stSidebar"] .stMarkdown h3 {font-size: 0.95rem;}
.ps-brand {font-weight: 700; font-size: 1.15rem; color: #1F4E79; letter-spacing: 0.04em; margin: 0.2rem 0 0 0;}
.ps-brand-sub {color: #6B7280; font-size: 0.78rem; margin-bottom: 0.9rem;}
.ps-status {font-size: 0.78rem; color: #4B5563; line-height: 1.5; border-top: 1px solid #E5E7EB; padding-top: 0.6rem; margin-top: 0.6rem;}

/* 指标 */
[data-testid="stMetric"] {background: #F8FAFC; border: 1px solid #E5E7EB; border-radius: 10px; padding: 0.6rem 0.9rem;}
[data-testid="stMetricLabel"] {color: #6B7280 !important; font-size: 0.78rem !important;}
[data-testid="stMetricValue"] {font-size: 1.25rem !important; font-weight: 600 !important; color: #1F2933 !important;}

/* 标签页 */
button[data-baseweb="tab"] {font-weight: 500; padding: 0.4rem 0.9rem;}
[data-baseweb="tab-highlight"] {background-color: #1F4E79 !important;}

/* 按钮 */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {border-radius: 6px; border: 1px solid #D1D5DB; padding: 0.35rem 0.9rem;}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {background: #1F4E79; border-color: #1F4E79;}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {background: #173B5C; border-color: #173B5C;}

/* 提示条：更克制 */
[data-testid="stAlert"] {border-radius: 8px; padding: 0.55rem 0.9rem;}

/* 输入框 */
[data-baseweb="input"] > div, [data-baseweb="select"] > div, [data-baseweb="textarea"] > div {border-radius: 6px;}

/* 聊天 */
[data-testid="stChatMessage"] {border-radius: 10px; padding: 0.6rem 0.9rem;}
[data-testid="stChatInput"] {border-radius: 10px;}

/* 数据表工具条淡化 */
[data-testid="stElementToolbar"] {opacity: 0.6;}
</style>
"""


def inject_style() -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)


def sidebar_status(status_lines: list[str] | None = None) -> None:
    """侧栏底部的状态小字（模型、价格基准）。"""
    if status_lines:
        st.sidebar.markdown('<div class="ps-status">' + "<br>".join(status_lines) + "</div>", unsafe_allow_html=True)


def page_header(title: str, subtitle: str | None = None) -> None:
    st.markdown(f"<h1>{title}</h1>", unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<div style="color:#6B7280;font-size:0.9rem;margin:-0.2rem 0 1rem 0">{subtitle}</div>', unsafe_allow_html=True)
