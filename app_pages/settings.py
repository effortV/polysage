from __future__ import annotations

import re

import streamlit as st

from polysage import llm, ui
from polysage.config import ROOT, reload_settings, settings
from polysage.pipeline import task
from polysage.sources import elsevier

ui.page_header("设置", "模型、接口与任务书。")

st.subheader("接口状态")
for name, ok in settings.status().items():
    st.write(("✅ " if ok else "⚪ ") + name)

def _set_env(pairs: dict[str, str]) -> None:
    """写入 .env（存在则替换该行，否则追加）。"""
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    for k, v in pairs.items():
        if v is None:
            continue
        if re.search(rf"^{k}=.*$", text, flags=re.M):
            text = re.sub(rf"^{k}=.*$", f"{k}={v}", text, flags=re.M)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + f"{k}={v}\n"
    env.write_text(text, encoding="utf-8")
    reload_settings()


with st.container(border=True):
    st.markdown("##### 对话模型")
    st.caption("两套 OpenAI 兼容接口可切换；向量与重排固定使用 SiliconFlow bge-m3。")
    profile = st.radio("当前使用", ["siliconflow", "zju"], index=0 if settings.llm_profile != "zju" else 1, horizontal=True,
                       format_func={"siliconflow": f"SiliconFlow · {settings.sf_chat_model}", "zju": f"ZJU · {settings.zju_chat_model}"}.get)
    if profile != settings.llm_profile and st.button("切换到该模型"):
        _set_env({"LLM_PROFILE": profile})
        st.success(f"已切换为 {profile}")
        st.rerun()
    c_sf, c_zju = st.columns(2)
    with c_sf:
        st.markdown("###### SiliconFlow")
        sf_key = st.text_input("SILICONFLOW_API_KEY", type="password", placeholder="sk-…（留空不改）")
        sf_model = st.text_input("对话模型", value=settings.sf_chat_model, key="sf_model")
        if st.button("保存 SiliconFlow"):
            _set_env({"SILICONFLOW_API_KEY": sf_key or None, "SILICONFLOW_CHAT_MODEL": sf_model})
            st.success("已保存")
            st.rerun()
    with c_zju:
        st.markdown("###### ZJU")
        zju_url = st.text_input("ZJU_BASE_URL", value=settings.zju_base_url)
        zju_key = st.text_input("ZJU_API_KEY", type="password", placeholder="留空不改")
        zju_model = st.text_input("模型", value=settings.zju_chat_model, key="zju_model")
        zju_think = st.toggle("该服务支持 enable_thinking 参数", value=settings.zju_supports_thinking, help="不确定就关；开了若报 400 就关掉")
        if st.button("保存 ZJU"):
            _set_env({"ZJU_BASE_URL": zju_url, "ZJU_API_KEY": zju_key or None, "ZJU_CHAT_MODEL": zju_model, "ZJU_SUPPORTS_THINKING": "1" if zju_think else "0"})
            st.success("已保存")
            st.rerun()
    b1, b2, b3 = ui.hrow(3)
    with b1:
        st.caption(f"当前：{settings.llm_profile} / {settings.chat_model}")
    with b2:
        if st.button("测试对话模型"):
            ok, msg = llm.ping()
            (st.success if ok else st.error)(msg)
    with b3:
        if st.button("测试 Scopus"):
            ok, msg = elsevier.ping()
            (st.success if ok else st.error)(msg)

st.subheader("任务书")
t = task.load()
c1, c3, c4 = st.columns(3)
t["product"]["name"] = c1.text_input("产品", t["product"]["name"])
t["product"]["structure"] = c3.selectbox("结构", ["mono", "ABA"], index=0 if t["product"]["structure"] == "mono" else 1)
th = c4.number_input("膜厚 μm（0 = 待定）", 0, 500, int(t["product"].get("thickness_um") or 0))
t["product"]["thickness_um"] = th or None
st.markdown("##### 现配方（合计 100%）")
cols = st.columns(4)
for i, (code, v) in enumerate(t["current_formulation"].items()):
    with cols[i % 4]:
        v["pct"] = st.number_input(f"{code} %", 0.0, 100.0, float(v["pct"]), key=f"pct_{code}")
        v["grade"] = st.text_input(f"{code} 牌号", v.get("grade", ""), key=f"grade_{code}")
t["formulation_note"] = st.text_input("说明", t.get("formulation_note", ""))
c5, c6, c7 = st.columns(3)
t["cost"]["current"] = c5.number_input("现成本 元/吨", 0, 50000, int(t["cost"]["current"]))
t["cost"]["limit"] = c6.number_input("成本上限", 0, 50000, int(t["cost"]["limit"]))
t["cost"]["target"] = c7.number_input("目标成本", 0, 50000, int(t["cost"]["target"]))
if st.button("保存任务书"):
    total = sum(v["pct"] for v in t["current_formulation"].values())
    if abs(total - 100) > 0.5:
        st.error(f"现配方合计 {total}% ≠ 100%")
    else:
        task.save(t)
        from polysage.formulation import constraints as C

        c = C.load()
        c["base_formulation"] = {k: v["pct"] for k, v in t["current_formulation"].items() if v["pct"]}
        c["cost_limit"], c["cost_target"] = t["cost"]["limit"], t["cost"]["target"]
        C.save(c)
        st.success("已保存任务书并同步 base 配方与成本上限到约束文件")
