"""Streamlit 版本兼容层：让页面在 1.45（Anaconda 自带）到 1.6x 都能跑。

新版才有的参数（container(horizontal=)、expander/status(type=)、chat_input(submit_mode=)、width="stretch"）
在旧版会直接报 TypeError，这里统一判断后再传。
"""
from __future__ import annotations

import html
import inspect
import time
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
/* logo 放大：1.45 的 img 是 data-testid=stLogo，1.6x 改成 stSidebarLogo，类名 .stLogo 两版都有 */
img.stLogo, img[data-testid="stLogo"], img[data-testid="stSidebarLogo"] {height: 4rem !important; width: auto !important; max-width: 100% !important;}
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

/* 侧栏「后台运行」面板 */
.ps-activity {font-size: 0.76rem; color: #4B5563; line-height: 1.5; border-top: 1px solid #E5E7EB; padding-top: 0.6rem; margin-top: 0.8rem;}
.ps-activity-head {display: flex; justify-content: space-between; font-weight: 600; color: #1F2933; margin-bottom: 0.25rem;}
.ps-activity-up {font-weight: 400; color: #9CA3AF;}
.ps-activity-row {margin: 0.1rem 0;}
.ps-dot {display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; background: #9CA3AF; vertical-align: 1px;}
.ps-dot.run {background: #1F4E79; animation: ps-pulse 1.4s ease-in-out infinite;}
.ps-dot.err {background: #B91C1C;}
.ps-dot.pause {background: #D97706;}
.st-key-ps_ctl .stButton > button {padding: 0.15rem 0.6rem; font-size: 0.76rem; min-height: 1.7rem; line-height: 1.2;}
@keyframes ps-pulse {0%, 100% {opacity: 1;} 50% {opacity: 0.3;}}
.ps-activity-log {margin-top: 0.4rem; padding-top: 0.35rem; border-top: 1px dashed #E5E7EB; color: #6B7280; font-family: ui-monospace, Consolas, monospace; font-size: 0.7rem; line-height: 1.45;}
.ps-activity-log .t {color: #9CA3AF; margin-right: 6px;}
.ps-activity-log .bad {color: #B91C1C;}
</style>
"""


def inject_style() -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)


def _activity_html(ov: dict) -> str:
    from . import activity

    esc = html.escape
    rows: list[str] = []
    cur = ov.get("current")
    if cur and cur.get("cancelling"):
        rows.append(f'<div class="ps-activity-row"><span class="ps-dot err"></span>{esc(cur["label"])} · 正在取消</div>')
    elif cur and cur.get("paused"):
        rows.append(f'<div class="ps-activity-row"><span class="ps-dot pause"></span>{esc(cur["label"])} · 已暂停</div>')
    elif cur:
        rows.append(f'<div class="ps-activity-row"><span class="ps-dot run"></span>{esc(cur["label"])} · 已运行 {activity.fmt_seconds(cur["elapsed"])}</div>')
    elif ov.get("busy"):
        rows.append('<div class="ps-activity-row"><span class="ps-dot run"></span>处理中</div>')
    else:
        rows.append('<div class="ps-activity-row"><span class="ps-dot"></span>空闲</div>')
    for ln in ov.get("lines", []):
        parts = [f'{ln["name"]} {ln["total"]} 次']
        if ln["inflight"]:
            parts.append(f'进行中 {ln["inflight"]}')
        if ln["errors"]:
            parts.append(f'失败 {ln["errors"]}')
        if ln["last_seconds"] is not None:
            parts.append(f'最近 {activity.fmt_seconds(ln["last_seconds"])}')
        rows.append('<div class="ps-activity-row">' + " · ".join(parts) + "</div>")
    pr = ov.get("price") or {}
    auto = f'每 {pr["auto_days"]} 天' if pr.get("auto_days") else "手动"
    rows.append(f'<div class="ps-activity-row">参考价刷新 {auto}' + (f' · 上次 {esc(pr["last"][5:])}' if pr.get("last") else "") + "</div>")
    if ov.get("tunnel_url"):
        u = esc(ov["tunnel_url"])
        rows.append(f'<div class="ps-activity-row">外网地址 <a href="{u}" target="_blank">{u.replace("https://", "")}</a></div>')
    log: list[str] = []
    for ev in ov.get("events", []):
        cat = ev["category"]
        if cat == "llm":
            text = f'模型调用 {activity.fmt_seconds(ev["seconds"])}'
        elif cat == "search":
            text = f'检索 {ev["label"]} {activity.fmt_seconds(ev["seconds"])}'
        elif cat in ("embed", "rerank"):
            text = f'{activity.CATEGORY_NAMES[cat]} {activity.fmt_seconds(ev["seconds"])}'
        else:
            text = ev["label"]
        if not ev["ok"]:
            text += " 失败"
        log.append(f'<div class="{"bad" if not ev["ok"] else ""}"><span class="t">{activity.fmt_clock(ev["at"])}</span>{esc(text)}</div>')
    head = ('<div class="ps-activity-head"><span>后台运行</span>'
            f'<span class="ps-activity-up">服务已运行 {activity.fmt_seconds(ov.get("uptime", 0))}</span></div>')
    body = "".join(rows) + ('<div class="ps-activity-log">' + "".join(log) + "</div>" if log else "")
    return f'<div class="ps-activity">{head}{body}</div>'


def _activity_controls() -> None:
    """面板下的两个小按钮：开始/暂停/继续，删除（取消任务并清空最近事件）。"""
    from . import activity, jobs

    box = st.container(key="ps_ctl")  # 容器带 key → class st-key-ps_ctl，便于只给这两个按钮缩小样式
    c1, c2 = box.columns(2)
    running = jobs.is_running()
    changed = False
    if running and jobs.is_paused():
        if c1.button("继续", key="ps_act_resume", help="继续被暂停的任务", **_WIDE_BTN):
            jobs.resume()
            changed = True
    elif running:
        if c1.button("暂停", key="ps_act_pause", help="在下一次模型/检索调用前暂停", **_WIDE_BTN):
            jobs.pause()
            changed = True
    else:
        if c1.button("开始", key="ps_act_start", help="从未完成的阶段继续跑研发流水线 ①～⑥", **_WIDE_BTN):
            jobs.start_discovery()
            changed = True
    if c2.button("删除", key="ps_act_delete", help="取消当前任务并清空最近事件", **_WIDE_BTN):
        jobs.cancel()
        activity.clear_events()
        changed = True
    if changed:
        time.sleep(0.3)   # 给后台线程一点时间切换状态，再整页重跑刷新按钮与面板
        st.rerun()


_WIDE_BTN: dict[str, Any] = {"width": "stretch"} if _supports_str_width() else {"use_container_width": True}


@st.cache_data(ttl=2, max_entries=1, show_spinner=False)
def _cached_activity_overview() -> dict[str, Any]:
    """Short-lived cache for sidebar status reads shared by all sessions."""
    from . import activity

    return activity.overview()


def sidebar_activity() -> None:
    """侧栏底部「后台运行」面板：整个服务进程的任务、模型调用、检索与最近事件。

    有任务或调用进行中时每 3 s 刷新一次，否则每 10 s；只重跑这一小块，不重跑页面。
    """
    from . import activity, jobs

    busy = activity.busy() or jobs.is_running()

    @st.fragment(run_every=3 if busy else 10)
    def _panel() -> None:
        slot = st.empty()        # 状态占位：先处理按钮点击，再填状态，点完立刻反映
        _activity_controls()
        slot.markdown(_activity_html(_cached_activity_overview()), unsafe_allow_html=True)

    with st.sidebar:
        _panel()


def password_gate(password: str | None) -> None:
    """访问口令：没设口令直接放行；设了就在本会话首次打开时要求输入，之后不再问。"""
    import hmac

    if not password or st.session_state.get("_authed"):
        return
    st.markdown("<h1>HDU×恒诺 - 包装膜（AI4S）</h1>", unsafe_allow_html=True)
    with st.form("login"):
        pw = st.text_input("访问口令", type="password")
        ok = st.form_submit_button("进入", type="primary")
    if ok:
        if hmac.compare_digest(pw.encode("utf-8"), password.encode("utf-8")):
            st.session_state["_authed"] = True
            st.rerun()
        st.error("口令不对。")
    st.stop()


def page_header(title: str, subtitle: str | None = None) -> None:
    st.markdown(f"<h1>{title}</h1>", unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<div style="color:#6B7280;font-size:0.9rem;margin:-0.2rem 0 1rem 0">{subtitle}</div>', unsafe_allow_html=True)
