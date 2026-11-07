"""让配方助手自己调度后台工作：找新材料、跑研发流水线、更新行情，以及查进度。

这些活一跑就是几分钟到一小时，放后台线程跑（同一时间只跑一个），结果直接写进原料库 / 价格卡 / 知识库，
之后再算方案就自动用上。
"""
from __future__ import annotations

import time
from typing import Any

from .. import jobs
from .tools import tool

JOB_LABELS = {"research_materials": "找新材料", "research_literature": "文献与专利调研", "discovery": "研发流水线（全流程）",
              "market_refresh": "更新行情", "aba_round": "跑一整轮（行情→新料→试验方案→Word）"}
jobs.JOB_NAMES.update(JOB_LABELS)
_LAST: dict[str, dict[str, Any]] = {}       # 每类任务最近一次的结果摘要（给 background_status 用）


def _new_materials_job() -> dict[str, Any]:
    from .. import scout
    from ..aba import optimize as O

    out = scout.discover("", depth="标准", max_new=8)
    added = [f"{a['code']} {a['name']}（{a['grade'] or '无牌号'}）" + (f" 网查价 {a['price']:.0f}" if a.get("price") else " 待询价")
             for a in out["added"]]
    best = O.run({"md_req": 1.0, "td_req": 1.0, "use_new": True})
    better = [ev for items in (best["groups"].get("加新料") or {}).values() for ev in items]
    summary = {"at": time.strftime("%Y-%m-%d %H:%M"), "added": added, "notes": out["notes"][:4],
               "better": [f"{ev['text']}：{ev['cost']:.0f} 元/吨（比只用现有料省 {-ev['vs_current']:.0f}）" for ev in better[:3]]}
    _LAST["research_materials"] = summary
    return summary


def _literature_job() -> dict[str, Any]:
    from ..pipeline import runner, state

    runner.run_discovery(["collect", "mechanism"], echo=None, resume=False)
    st = state.load()["stages"]
    summary = {"at": time.strftime("%Y-%m-%d %H:%M"), "kept": st.get("collect", {}).get("n_kept"),
               "ingested": st.get("collect", {}).get("n_ingested"), "cards": st.get("collect", {}).get("n_cards"),
               "report": "现配方机理报告已更新" if st.get("mechanism", {}).get("status") == "done" else "机理报告未完成"}
    _LAST["research_literature"] = summary
    return summary


def _market_job() -> dict[str, Any]:
    from .. import daily_quotes, forecast

    summary: dict[str, Any] = {"at": time.strftime("%Y-%m-%d %H:%M")}
    try:
        web = daily_quotes.web_market_quotes(["LLDPE", "LDPE", "HDPE"], depth="标准")
        summary["web"] = {fam: f"{v.get('rows', 0)} 条" for fam, v in (web or {}).items()}
        applied = daily_quotes.apply_web_market(["LLDPE", "LDPE", "HDPE"])
        if applied:
            summary["price_card"] = {a["family"]: (f"{a['price']:.0f}（{a['text']}）" if a.get("applied") else f"没改（{a['text']}）")
                                     for a in applied}
    except Exception as e:  # noqa: BLE001
        summary["web_error"] = str(e)[:120]
    try:
        fc = forecast.run()                                       # 放在价格卡更新之后：预判的“现价”用新的
        summary["forecast"] = {fam: (f"{r['current']:.0f}" if r.get("current") else "-")
                               + f" → 1 个月 {r.get('price_1m') or '-'}（{r.get('direction') or ''}）"
                               for fam, r in (fc or {}).items()}
    except Exception as e:  # noqa: BLE001
        summary["forecast_error"] = str(e)[:120]
    try:
        mat = daily_quotes.web_material_quotes(["R1", "FL", "RL"], depth="快", pages_per_query=2)
        daily_quotes.apply_web_estimates(["RL"])          # 没有实价的辅料：网查最低到厂价写成估计价（R1、FL 有甲方实价，不动）
        summary["materials"] = {c: f"{v.get('rows', 0)} 条" + (f"，最低到厂 {v['best']['landed']:.0f}" if v.get("best") else "")
                                for c, v in (mat or {}).items()}
    except Exception as e:  # noqa: BLE001
        summary["materials_error"] = str(e)[:120]
    _LAST["market_refresh"] = summary
    return summary


def _start(name: str, fn) -> str:
    running = jobs.info()
    if running:
        label = JOB_LABELS.get(running["name"], running["name"])
        return (f"后台正在跑「{label}」（已 {running['elapsed'] / 60:.0f} 分钟），同一时间只跑一个。"
                "等它跑完再开新的；进度可以随时问我。")
    if jobs.start(name, fn):
        return f"已在后台开始「{JOB_LABELS[name]}」。"
    return "没能启动后台任务，请稍后再试。"


@tool("start_research", "在后台启动调研：scope=新材料（约 3~8 分钟：上网找能买到的新料入原料库，再看它们能不能比现有料更省）；"
      "scope=文献专利（约 30~60 分钟：研发流水线的文献/专利采集 + 机理报告，结果进知识库）；scope=全部（研发流水线全流程，约 1 小时）。"
      "用户问“有没有最新的材料 / 新技术 / 文献专利怎么说”时用；启动后告诉用户大概多久，进度用 background_status 查。",
      {"scope": {"type": "string", "enum": ["新材料", "文献专利", "全部"]}}, ["scope"])
def start_research(scope: str = "新材料"):
    if scope == "文献专利":
        msg = _start("research_literature", _literature_job)
        tip = "大约 30~60 分钟，跑完后文献卡和机理报告在「资料库」里，我回答时会自动引用。"
    elif scope == "全部":
        from ..pipeline import runner

        msg = _start("discovery", lambda: runner.run_discovery(None, echo=None, resume=True))
        tip = "全流程约 1 小时：文献采集 → 机理报告 → 找新料 → 分档方案 → 报告 → 首轮试验方案。"
    else:
        msg = _start("research_materials", _new_materials_job)
        tip = "大约 3~8 分钟：找到的料会带来源网址进原料库，跑完我会对比它们能不能比现有料更省。"
    return msg + tip, []


@tool("refresh_market", "在后台更新行情（约 5~10 分钟）：拉 LLDPE 期货并做价格预判、网查 LLDPE/LDPE/HDPE 同类报价、网查再生料和填充料报价。"
      "用户问“现在多少钱 / 最新价格 / 行情怎么样”且价格卡已经旧了时用；等结果的同时，可以先用 aba_prices 给按期货修正的估算。", {})
def refresh_market():
    return _start("market_refresh", _market_job) + ("大约 5~10 分钟。贸易商日报才是正式价；日报超过 3 周没更新时，价格卡按网查的华东市场价"
                                                     "中位数计（厂家挂牌价、机构评述不算）；华东市场价停更时，按生意社基准价的涨跌推到最新一天。"
                                                     "进度用 background_status 查。"), []


@tool("background_status", "看后台任务：正在跑什么、跑了多久，以及最近几次找新材料 / 文献调研 / 更新行情的结果摘要。", {})
def background_status():
    from ..pipeline import state

    lines = []
    running = jobs.info()
    if running:
        lines.append(f"正在跑：{JOB_LABELS.get(running['name'], running['name'])}，已 {running['elapsed'] / 60:.0f} 分钟"
                     + ("（已暂停）" if running.get("paused") else ""))
    else:
        lines.append("后台空闲。")
    for name, label in JOB_LABELS.items():
        res = jobs.result(name)
        if res and res["done"] and res.get("error"):
            lines.append(f"「{label}」上次失败：{res['error'][:120]}")
        last = _LAST.get(name) or ((res or {}).get("result") if res and res["done"] else None)
        if isinstance(last, dict) and last:
            lines.append(f"「{label}」最近一次（{last.get('at', '')}）：{ {k: v for k, v in last.items() if k != 'at'} }")
    st = state.load().get("stages", {})
    done = [state.STAGE_NAMES.get(k, k) for k, v in st.items() if v.get("status") == "done"]
    if done:
        lines.append("研发流水线已完成的步骤：" + "、".join(done))
    return "\n".join(lines), []
