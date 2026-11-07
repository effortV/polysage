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
/* 客户用：不显示开发者菜单、部署按钮、顶部彩条 */
#MainMenu, footer, .stAppDeployButton, [data-testid="stDecoration"] {display: none !important;}
header[data-testid="stHeader"] {background: transparent;}
.block-container {padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1320px;}
[data-testid="stCaptionContainer"] {color: #6B7480 !important;}

/* logo：1.45 是 data-testid=stLogo，1.6x 是 stSidebarLogo，类名 .stLogo 两版都有 */
img.stLogo, img[data-testid="stLogo"], img[data-testid="stSidebarLogo"] {height: 3.6rem !important; width: auto !important; max-width: 100% !important;}

/* 侧栏「后台运行」面板（自绘 HTML） */
.ps-activity {font-size: 0.76rem; color: #4B5563; line-height: 1.5; border-top: 1px solid #E2E6EC; padding-top: 0.6rem; margin-top: 0.8rem;}
.ps-activity-head {display: flex; justify-content: space-between; font-weight: 600; color: #1C2430; margin-bottom: 0.25rem;}
.ps-activity-up {font-weight: 400; color: #9CA3AF;}
.ps-activity-row {margin: 0.1rem 0;}
.ps-dot {display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; background: #9CA3AF; vertical-align: 1px;}
.ps-dot.run {background: #1E4B7A; animation: ps-pulse 1.4s ease-in-out infinite;}
.ps-dot.err {background: #B3261E;}
.ps-dot.pause {background: #B86E00;}
.st-key-ps_ctl .stButton > button {padding: 0.15rem 0.6rem; font-size: 0.76rem; min-height: 1.7rem; line-height: 1.2;}
@keyframes ps-pulse {0%, 100% {opacity: 1;} 50% {opacity: 0.3;}}
.ps-activity-log {margin-top: 0.4rem; padding-top: 0.35rem; border-top: 1px dashed #E2E6EC; color: #6B7480; font-family: ui-monospace, Consolas, monospace; font-size: 0.7rem; line-height: 1.45;}
.ps-activity-log .t {color: #9CA3AF; margin-right: 6px;}
.ps-activity-log .bad {color: #B3261E;}
/* 配方助手左边的历史对话：标题左对齐、一行放不下就省略 */
.st-key-chat_hist .stButton button {justify-content: flex-start; text-align: left; min-width: 0;}
.st-key-chat_hist .stButton button div, .st-key-chat_hist .stButton button p {min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
/* 写登录 cookie 的小脚本：藏起来，不占位置 */
.st-key-hn_login_js, [data-testid="stLayoutWrapper"]:has(> .st-key-hn_login_js) {display: none !important;}
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


HAS_HTML_JS = _has_param(st.html, "unsafe_allow_javascript")


def _login_cookie_js(token: str | None) -> None:
    """在浏览器里写（或清掉）记住登录的 cookie。脚本放在藏起来的容器里，不占页面位置。"""
    from . import auth

    val, age = (token, auth.REMEMBER_DAYS * 86400) if token else ("", 0)
    js = ("<script>(function(){var s=location.protocol==='https:'?'; Secure':'';"
          f"document.cookie='{auth.COOKIE}={val}; Max-Age={age}; Path=/; SameSite=Lax'+s;}})();</script>")
    with st.container(key="hn_login_js"):
        if HAS_HTML_JS:
            st.html(js, unsafe_allow_javascript=True)
        else:  # 旧版：组件 iframe 与页面同源，写的是同一个站点的 cookie
            import streamlit.components.v1 as components

            components.html(js, height=0)


def _cookie(name: str) -> str | None:
    try:
        return st.context.cookies.get(name)
    except Exception:  # noqa: BLE001  旧版没有 st.context.cookies / 测试环境
        return None


def password_gate(_legacy: str | None = None) -> None:
    """访问密码：没设直接放行；设了就要求输入（比对哈希）。

    勾了“记住”，这台设备 30 天内打开网页不用再输；改了访问密码，所有设备记住的登录都失效，重新登录即可。
    """
    from . import auth

    if not auth.password_required():
        return
    mark = auth.session_mark()
    if st.session_state.get("_authed") != mark and not st.session_state.get("_logged_out"):
        if auth.token_ok(_cookie(auth.COOKIE)):
            st.session_state["_authed"] = mark
    if st.session_state.get("_authed") == mark:
        if st.session_state.get("_login_cookie"):
            _login_cookie_js(st.session_state["_login_cookie"])
        return
    st.session_state.pop("_login_cookie", None)
    if st.session_state.pop("_clear_login_cookie", False) or _cookie(auth.COOKIE):
        _login_cookie_js(None)        # 退出登录 / 密码改了：把这台设备上失效的记住登录清掉
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.space("large")
        st.markdown("### HDU×恒诺 · 包装膜配方平台")
        st.caption("请输入访问密码")
        with st.form("login", border=True):
            pw = st.text_input("访问密码", type="password", label_visibility="collapsed", placeholder="访问密码")
            remember = st.checkbox(f"这台设备 {auth.REMEMBER_DAYS} 天内免登录", value=True,
                                   help="公用电脑请取消勾选。改了访问密码后，记住的登录会失效，要重新登录。")
            ok = st.form_submit_button("进入", type="primary", width="stretch")
        if ok:
            if auth.check_password(pw):
                st.session_state["_authed"] = mark
                st.session_state.pop("_logged_out", None)
                if remember:
                    st.session_state["_login_cookie"] = auth.issue_token()
                st.rerun()
            st.error("密码不对。")
    st.stop()


def logout() -> None:
    """退出这台设备的登录（清掉记住的 30 天登录）。"""
    for k in ("_authed", "_login_cookie"):
        st.session_state.pop(k, None)
    st.session_state["_logged_out"] = True
    st.session_state["_clear_login_cookie"] = True


def keep_login_after_password_change() -> None:
    """改密码的人自己不用马上重登：给这个会话和这台设备换成新密码的登录。"""
    from . import auth

    st.session_state["_authed"] = auth.session_mark()
    st.session_state["_login_cookie"] = auth.issue_token()


def page_header(title: str, subtitle: str | None = None) -> None:
    st.title(title, anchor=False)
    if subtitle:
        st.caption(subtitle)
