"""流水线 ④⑥⑦⑧⑨ 的 ABA 版本：分档方案、首轮试验、校准、下一轮、复盘。

旧的单层 / 四项指标版本（stage_design、stage_experiment 里的同名阶段）不再从流水线调用。
"""
from __future__ import annotations

from typing import Any, Callable

from .. import db, kb, llm
from ..config import KB
from . import state, task

REVIEW_PROMPT = (
    "任务：{brief}\n\n这是 ABA 三层膜第 {n} 轮小试：实测（纵向/横向拉断力 N、透光档 0 一般/1 好/2 很好）与模型预测的对比：\n{table}\n\n"
    "校准情况：{cal}\n\n请写复盘纪要，固定标题：\n## 哪些预测对了、哪些错了（错在哪一项、偏多少）\n"
    "## 系数表需要怎么改（哪类料、哪个方向）\n## 下一轮推荐方向（往哪调填充、厚度、再生比例、A 层占比）\n## 需要补测或重测的组\n"
    "只基于数据说话，不编造。"
)


def run_design(echo: Callable[[str], None] | None = None, **_: Any) -> dict[str, Any]:
    """④ 分档方案：性能不降，透光三档各给最低成本（现有料 + 加新料），入配方库并导出。"""
    from ..aba import optimize as O
    from ..aba import report as RP

    with state.running("design", echo):
        out = O.run(O.Query(md_req=1.0, td_req=1.0, grade_min=None, use_new=True, top_k=3))
        path = RP.export_xlsx(out)
        codes = RP.save_to_library(out, origin="流水线 ④", per_grade=2)
        rows = O.rows(out)
        state.log(f"评估 {out['n_evaluated']} 个 ABA 方案，入库 {len(codes)} 个", echo)
        summary = {"n_pool": out["n_evaluated"], "n_top": len(rows), "codes": codes, "outputs": [str(path)],
                   "top": [{"rank": i, "formula": r["配方"], "cost": r["元/吨"], "savings_pct": r["_ev"].get("saving_pct"),
                            "expected": f"{r['分组']}｜透光{r['透光要求']}｜纵 {r['纵向拉断力']}× 横 {r['横向拉断力']}×",
                            "risk": (r["依据"] or "")[:120], "confidence": r["把握"]} for i, r in enumerate(rows, 1)]}
        state.set_stage("design", **summary)
    return summary


def run_doe(echo: Callable[[str], None] | None = None, **_: Any) -> dict[str, Any]:
    """⑥ 首轮试验：还没有试验就出首轮（对照 + D-最优），已有就导出最新一轮的方案表。"""
    from ..aba import experiments as E

    with state.running("doe", echo):
        rows = E.trials()
        if rows:
            last = max(r["round"] for r in rows)
            path = E.export_round(last)
            state.log(f"已有 {len(rows)} 组试验，导出第 {last} 轮方案表", echo)
        else:
            r = E.first_round()
            path = r["path"]
            state.log(f"首轮试验 {len(r['trials'])} 组", echo)
        summary = {"n_design": len(E.trials()), "outputs": [str(path)]}
        state.set_stage("doe", **summary)
    return summary


def run_learn(echo: Callable[[str], None] | None = None, **_: Any) -> dict[str, Any]:
    """⑦ 校准：用已回填的实测把系数往数据拉。"""
    from ..aba import experiments as E

    with state.running("learn", echo):
        r = E.calibrate()
        if not r.get("ok"):
            raise ValueError(r.get("reason") or "还不能校准")
        state.log(f"已用 {r['n']} 组校准：纵 {r.get('md_N_ref')} N / 横 {r.get('td_N_ref')} N，透光命中 {r.get('grade_hit')}%", echo)
        summary = {k: r.get(k) for k in ("n", "md_N_ref", "td_N_ref", "md_rmse_pct", "td_rmse_pct", "grade_hit")}
        state.set_stage("learn", **summary, outputs=[])
    return summary


def run_scan(echo: Callable[[str], None] | None = None, **kw: Any) -> dict[str, Any]:
    """⑧ 下一轮：每档预测最省（利用）+ 离已做过的远（探索）+ 现配方对照。"""
    from ..aba import experiments as E

    with state.running("scan", echo):
        r = E.next_round(kw or {"md_req": 1.0, "td_req": 1.0})
        state.log(f"第 {r['round']} 轮推荐 {len(r['trials'])} 组", echo)
        summary = {"round": r["round"], "n": len(r["trials"]), "outputs": [r["path"]]}
        state.set_stage("scan", **summary)
    return summary


def run_review(round_no: int = 1, echo: Callable[[str], None] | None = None, **_: Any) -> dict[str, Any]:
    """⑨ 复盘：预测 vs 实测 → 复盘纪要（08_复盘）。"""
    from ..aba import experiments as E
    from ..aba import rules as R

    t = task.load()
    with state.running("review", echo):
        df = E.compare()
        if df.empty:
            raise ValueError("还没有回填的实测，先到「实验与模型」上传数据记录")
        table = df.to_string(index=False)
        cal = R.calibration()
        try:
            res = llm.chat([{"role": "system", "content": "你是项目技术负责人。"},
                            {"role": "user", "content": REVIEW_PROMPT.format(brief=task.brief(t), n=round_no, table=table[:12000],
                                                                            cal=cal or "未校准")}],
                           temperature=0.2, max_tokens=3000, thinking=False)
            content = res.content
        except (llm.LLMError, llm.LLMNotConfigured):
            content = "（对话模型不可用：以下为预测与实测对照表）\n\n" + table
        path = KB["review"] / f"ABA第{round_no}轮复盘_{db.now()[:10]}.md"
        kb.write_text(path, f"# ABA 第 {round_no} 轮复盘\n\n{content}\n")
        summary = {"round": round_no, "n_samples": len(df), "outputs": [str(path)]}
        state.set_stage("review", **summary)
    return summary
