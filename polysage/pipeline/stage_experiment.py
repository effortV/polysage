"""⑥～⑨ 实验闭环：首轮 DOE 方案 → 数据回灌与建模 → 扫描候选空间与推荐（LLM 复核）→ 复盘。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .. import db, kb, llm
from ..config import KB
from ..formulation import constraints as C
from ..formulation import cost as COST
from ..formulation import library as L
from ..formulation.materials import list_materials
from ..ml import dataset as D
from ..ml import doe as DOE
from ..ml import models as M
from ..ml import optimize as O
from . import state, task

DESIGN_PATH = KB["experiment"] / "首轮试验方案.xlsx"
RECO_PATH = KB["experiment"] / "推荐配方.xlsx"
VARIABLES = ["LL", "LLC", "mLL", "LD", "HD", "R1", "RL", "POE"]

REVIEW_RECO_PROMPT = (
    "任务：{brief}\n\n模型扫描候选空间后给出下列推荐（利用型 = 预测过关且便宜；探索型 = 不确定度大但可能更便宜）。"
    "请结合材料卡与约束逐条复核：① 化学/加工合理性（膜泡稳定、挤出压力、粘连、晶点）；② 材料可得性与货源；③ 是否接近训练域边界；"
    "给出“建议试 / 建议改 / 建议弃”与理由（引用材料卡）、观察点。输出 JSON：{{\"items\": [{{\"index\": 序号, \"verdict\": \"试/改/弃\", "
    "\"reason\": \"...\", \"watch\": \"...\", \"modification\": \"若建议改，给出改法\"}}], \"summary\": \"下一轮总体建议\"}}\n\n"
    "材料卡摘要：\n{materials}\n\n推荐：\n{recs}"
)

REVIEW_ROUND_PROMPT = (
    "任务：{brief}\n\n这是第 {n} 轮小试的实测数据（按样品聚合）与建模前的预测/定性预期对比：\n{table}\n\n"
    "过关阈值：{thr}\n\n请写复盘纪要，固定标题：\n## 哪些预测对了、哪些错了（错在哪个性能）\n## 需要修改的材料卡结论\n"
    "## 约束是否需要调整\n## 下一轮推荐方向\n## 需更新的卡片清单\n只基于数据说话，不编造。"
)


def doe_round1(priority: list[dict[str, float]] | None = None, n_points: int = 24, echo: Callable[[str], None] | None = None) -> Path:
    t = task.load()
    design = DOE.first_round(n_points=n_points, variables=VARIABLES, priority=priority or [])
    cov = DOE.coverage_report(design, VARIABLES)
    with pd.ExcelWriter(DESIGN_PATH, engine="openpyxl") as xw:
        design.to_excel(xw, sheet_name="试验配方", index=False)
        pd.DataFrame([{"变量": k, **v} if isinstance(v, dict) else {"变量": k, "值": v} for k, v in cov.items()]).to_excel(xw, sheet_name="覆盖度", index=False)
        pd.DataFrame({"说明": [
            "同机台、同厚度、同线速、同吹胀比、同温度曲线；每个配方稳定 20～30 分钟后取样",
            "整轮使用同一批再生料并留样；批号记入数据表 R1_batch / RL_batch",
            "AD（PPA+抗氧母料）固定 1%；EVA / 二级料 / C8 茂金属放第二轮",
            "先测现用膜 base（sample_id 以 S0 开头），四项各 ≥5 条，记录单次测量值",
            "数据按 06_实验数据/数据表模板.csv 回灌，一行一次测量",
        ]}).to_excel(xw, sheet_name="执行要求", index=False)
    D.write_template()
    state.log(f"首轮试验方案 {len(design)} 个配方 → {DESIGN_PATH.name}", echo)
    return DESIGN_PATH


def count_design() -> int:
    if not DESIGN_PATH.exists():
        return 0
    return len(pd.read_excel(DESIGN_PATH, sheet_name="试验配方"))


def run_doe(echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    with state.running("doe", echo):
        priority = [f["components"] for f in L.list_formulations() if f["status"] in ("候选", "推荐") and f.get("priority") and str(f["priority"]).isdigit() and int(f["priority"]) <= 8]
        path = doe_round1(priority=priority, echo=echo)
        summary = {"n_design": count_design(), "outputs": [str(path), str(D.TEMPLATE_PATH)]}
        state.set_stage("doe", **summary)
    return summary


def run_learn(echo: Callable[[str], None] | None = None, data_path: Path | None = None) -> dict[str, Any]:
    with state.running("learn", echo):
        df = D.load(data_path)
        if df.empty:
            raise ValueError(f"数据表为空：{data_path or D.DATA_PATH}")
        issues = D.qc(df)
        for i in issues:
            state.log(f"质检：{i}", echo)
        mats = {m["code"]: m for m in list_materials(active_only=False)}
        reports, summary = M.train_all(df, mats=mats)
        lines = [f"# 模型验证报告（{db.now()}）", "", f"样品数：{D.aggregate(df)['sample_id'].nunique()}；测量行数：{len(df)}", "",
                 "## 质检", *([f"- {i}" for i in issues] or ["- 无问题"]), "", "## 各性能模型（LOOCV）",
                 "| 性能 | 样本 | 选用模型 | RMSE | MAE | R²(LOOCV) | 验收 |", "|---|---|---|---|---|---|---|"]
        for r in reports:
            m = r.metrics.get(r.best, {})
            lines.append(f"| {r.target} | {r.n} | {r.best or '-'} | {m.get('rmse', float('nan')):.4g} | {m.get('mae', float('nan')):.4g} | "
                         f"{m.get('r2_loocv', float('nan')):.3f} | {'通过' if r.accepted else ('未通过' if r.accepted is False else '无 base')} {r.note} |")
        if "pass_accuracy" in summary:
            lines += ["", f"过关分类准确率（LOOCV）：{summary['pass_accuracy']:.0%}（验收 ≥ 80%）"]
        lines += ["", "## 过关阈值", *(f"- {k}: {v:.4g}" for k, v in (summary.get("thresholds") or {}).items()),
                  "", "说明：以 LOOCV 的 RMSE 为准，不看训练集 R²；验收未通过时先补数据，不调模型。"]
        path = KB["model"] / "模型验证报告.md"
        kb.write_text(path, "\n".join(lines) + "\n")
        out = {"n_rows": len(df), "issues": issues, "models": [{"target": r.target, "best": r.best, "rmse": r.metrics.get(r.best, {}).get("rmse"),
                                                                "accepted": r.accepted} for r in reports],
               "pass_accuracy": summary.get("pass_accuracy"), "outputs": [str(path)]}
        state.set_stage("learn", **out)
    return out


def run_scan(echo: Callable[[str], None] | None = None, n_exploit: int = 4, n_explore: int = 3, p_min: float = 0.8) -> dict[str, Any]:
    t = task.load()
    brief = task.brief(t)
    with state.running("scan", echo):
        mats = {m["code"]: m for m in list_materials(active_only=False)}
        res = O.recommend(n_exploit=n_exploit, n_explore=n_explore, p_min=p_min, mats=mats)
        rec = res["recommendations"].copy()
        targets = res["targets"]
        comp_cols = [c for c in D.COMP_COLS if c in rec and rec[c].abs().sum() > 0]
        rec.insert(0, "序号", range(1, len(rec) + 1))
        rec["配方"] = [COST.format_components({c: float(r[c]) for c in comp_cols if r[c]}) for _, r in rec.iterrows()]
        listing = "\n".join(f"{r['序号']}. [{r['type']}] {r['配方']} | 成本 {r['cost']:.0f} | P(过关) {r['p_pass']:.2f} | 训练域距离 {r['dist']:.3f} | "
                            + " ".join(f"{tg}={r[tg + '_mean']:.3g}±{(r.get(tg + '_sd') or 0):.2g}" for tg in targets)
                            for _, r in rec.iterrows())
        review = {"items": [], "summary": ""}
        try:
            review = llm.chat_json([{"role": "system", "content": "你是项目技术负责人，输出严格 JSON。"},
                                    {"role": "user", "content": REVIEW_RECO_PROMPT.format(brief=brief, materials=kb.cards_digest("material", max_chars=4000) or "（无材料卡）", recs=listing)}],
                                   max_tokens=3000, thinking=2048)
        except llm.LLMNotConfigured:
            state.log("LLM 未配置，跳过推荐复核", echo)
        except Exception as e:  # noqa: BLE001
            state.log(f"推荐复核失败：{e}", echo)
        items = {int(it.get("index")): it for it in (review.get("items") or []) if str(it.get("index", "")).isdigit()}
        rec["复核"] = [items.get(i, {}).get("verdict", "") for i in rec["序号"]]
        rec["理由"] = [items.get(i, {}).get("reason", "") for i in rec["序号"]]
        rec["观察点"] = [items.get(i, {}).get("watch", "") for i in rec["序号"]]
        rec["改法"] = [items.get(i, {}).get("modification", "") for i in rec["序号"]]
        cols = ["序号", "type", "配方", "cost", "p_pass", "dist", "min_margin", "uncertainty"] + [c + "_mean" for c in targets] + [c + "_sd" for c in targets] + ["复核", "理由", "观察点", "改法"] + comp_cols
        out_df = rec[[c for c in cols if c in rec]].rename(columns={"type": "类型", "cost": "成本(元/吨)", "p_pass": "P(过关)", "dist": "训练域距离",
                                                                     "min_margin": "最小裕度", "uncertainty": "不确定度"})
        # Pareto 前沿
        pf = O.pareto_front(res["candidates"])
        with pd.ExcelWriter(RECO_PATH, engine="openpyxl") as xw:
            out_df.round(3).to_excel(xw, sheet_name="推荐", index=False)
            pf[[c for c in ["cost", "p_pass", "min_margin", "theme"] + comp_cols if c in pf]].round(3).to_excel(xw, sheet_name="Pareto前沿", index=False)
            pd.DataFrame([{"阈值": k, "值": v} for k, v in res["thresholds"].items()]).to_excel(xw, sheet_name="过关阈值", index=False)
        # 存入配方库（状态：推荐）
        for _, r in rec.iterrows():
            code = L.next_code("R")
            try:
                L.save_formulation(code, {c: float(r[c]) for c in comp_cols if r[c]},
                                   predicted={tg: {"mean": float(r[tg + "_mean"]), "sd": float(r.get(tg + "_sd") or 0)} for tg in targets} | {"p_pass": float(r["p_pass"])},
                                   rationale=f"[{r['type']}] {r['理由']}", risks=r["观察点"], priority=str(r["序号"]), status="推荐", origin="流水线 ⑧ 扫描")
            except L.NotPurchasable:
                continue      # 买不到的料不进配方库
        md = [f"# 扫描推荐（{db.now()}）", "", res.get("note", ""), "", f"候选空间 {res['n_candidates']} 个；阈值：" + ", ".join(f"{k}={v:.4g}" for k, v in res["thresholds"].items()), "",
              "| 序号 | 类型 | 配方 | 成本 | P(过关) | 复核 | 理由 | 观察点 |", "|---|---|---|---|---|---|---|---|"]
        for _, r in rec.iterrows():
            md.append(f"| {r['序号']} | {r['type']} | {r['配方']} | {r['cost']:.0f} | {r['p_pass']:.2f} | {r['复核']} | {r['理由']} | {r['观察点']} |")
        md += ["", "## 总体建议", review.get("summary", "")]
        path_md = KB["experiment"] / "推荐配方.md"
        kb.write_text(path_md, "\n".join(md) + "\n")
        summary = {"n_candidates": res["n_candidates"], "n_recommended": len(rec), "note": res.get("note", ""),
                   "outputs": [str(RECO_PATH), str(path_md)]}
        state.set_stage("scan", **summary)
    return summary


def run_review(round_no: int = 1, echo: Callable[[str], None] | None = None) -> dict[str, Any]:
    """⑨ 复盘：实测 vs 预测（配方库中的 predicted）→ 复盘纪要 → 08_复盘。"""
    t = task.load()
    brief = task.brief(t)
    with state.running("review", echo):
        df = D.load()
        agg = D.aggregate(df)
        base = D.base_stats(agg)
        thr = D.thresholds(base) if base else {}
        if thr:
            agg["pass_flag"] = D.pass_flags(agg, thr)
        lib = L.list_formulations()
        rows = []
        for _, r in agg.iterrows():
            if str(r.get("source", "")) == "base":
                continue
            comps = {c: float(r[c]) for c in D.COMP_COLS if c in r and r[c]}
            code = str(r.get("scheme_code") or "")
            match = next((f for f in lib if f["code"] == code), None) if code else next((f for f in lib if f["components"] == comps), None)
            rows.append({"sample_id": r["sample_id"], "来源": r.get("source", ""), "方案": code, "配方": COST.format_components(comps), "预期/预测": (match or {}).get("predicted"),
                         **{tg: round(float(r[tg]), 3) for tg in D.PRIMARY_TARGETS if tg in r and pd.notna(r[tg])},
                         "过关": int(r.get("pass_flag", 0)) if thr else "无 base"})
        table = pd.DataFrame(rows).to_string()
        try:
            res = llm.chat([{"role": "system", "content": "你是项目技术负责人。"},
                            {"role": "user", "content": REVIEW_ROUND_PROMPT.format(brief=brief, n=round_no, table=table[:12000],
                                                                                  thr={k: round(v, 3) for k, v in thr.items()})}],
                           temperature=0.2, max_tokens=3000, thinking=4096)
            content = res.content
        except llm.LLMNotConfigured:
            content = "（LLM 未配置：以下为实测与过关表）\n\n" + table
        path = KB["review"] / f"第{round_no}轮复盘_{db.now()[:10]}.md"
        kb.write_text(path, f"# 第 {round_no} 轮复盘\n\n{content}\n")
        n_pass = int(agg["pass_flag"].sum()) if "pass_flag" in agg else 0
        summary = {"round": round_no, "n_samples": len(agg), "n_pass": n_pass, "outputs": [str(path)]}
        state.set_stage("review", **summary)
    return summary


def convergence_check(history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """停止条件：连续两轮最优过关配方成本改善 < 50 元/吨。history 为各轮 {round, best_pass_cost}。"""
    hist = history or state.load().get("rounds", [])
    if len(hist) < 3:
        return {"converged": False, "reason": "轮次不足"}
    a, b, c = (h.get("best_pass_cost") for h in hist[-3:])
    if None in (a, b, c):
        return {"converged": False, "reason": "存在无过关配方的轮次"}
    if (a - b) < 50 and (b - c) < 50:
        return {"converged": True, "reason": "连续两轮改善 < 50 元/吨"}
    return {"converged": False, "reason": f"最近两轮改善 {a - b:.0f} / {b - c:.0f} 元/吨"}


def record_round(round_no: int) -> dict[str, Any]:
    """实验回灌后记录本轮最优过关配方成本，用于收敛判断。"""
    df = D.load()
    agg = D.aggregate(df)
    base = D.base_stats(agg)
    best = None
    if base:
        thr = D.thresholds(base)
        agg["pass_flag"] = D.pass_flags(agg, thr)
        costs = []
        for _, r in agg[agg["pass_flag"] == 1].iterrows():
            comps = {c: float(r[c]) for c in D.COMP_COLS if c in r and r[c]}
            cst = COST.compute_simple(comps)
            if cst is not None and not str(r["sample_id"]).upper().startswith("S0"):
                costs.append(cst)
        best = min(costs) if costs else None
    st = state.load()
    rounds = [x for x in st.get("rounds", []) if x.get("round") != round_no]
    rounds.append({"round": round_no, "best_pass_cost": best, "at": db.now()})
    st["rounds"] = sorted(rounds, key=lambda x: x["round"])
    state.save(st)
    return {"round": round_no, "best_pass_cost": best, **convergence_check(st["rounds"])}
