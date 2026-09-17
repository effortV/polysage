"""流水线编排与命令行入口。

用法：
  python -m polysage.pipeline run                      # 发现阶段 ①～⑤（含 ⑥ 首轮 DOE）
  python -m polysage.pipeline run --stages collect,mechanism
  python -m polysage.pipeline learn --data 06_实验数据/data.csv   # ⑦ 回灌建模
  python -m polysage.pipeline scan                     # ⑧ 扫描推荐
  python -m polysage.pipeline review --round 1         # ⑨ 复盘
  python -m polysage.pipeline status
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from . import stage_collect, stage_design, stage_experiment, stage_mechanism, stage_report, stage_scout, state

DISCOVERY = ["collect", "mechanism", "scout", "design", "report", "doe"]


def _echo(line: str) -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8"), flush=True)


def run_stage(key: str, echo: Callable[[str], None] | None = _echo, **kw: Any) -> dict[str, Any]:
    if key == "collect":
        return stage_collect.run(echo, **kw)
    if key == "mechanism":
        return stage_mechanism.run(echo, **kw)
    if key == "scout":
        return stage_scout.run(echo, **kw)
    if key == "design":
        return stage_design.run(echo, **kw)
    if key == "report":
        return stage_report.run(echo)
    if key == "doe":
        return stage_experiment.run_doe(echo)
    if key == "learn":
        return stage_experiment.run_learn(echo, **kw)
    if key == "scan":
        return stage_experiment.run_scan(echo, **kw)
    if key == "review":
        return stage_experiment.run_review(echo=echo, **kw)
    raise KeyError(key)


def run_discovery(stages: list[str] | None = None, echo: Callable[[str], None] | None = _echo, resume: bool = True,
                  **kw: Any) -> dict[str, dict[str, Any]]:
    """跑发现阶段；resume=True 时跳过已完成的阶段。"""
    results = {}
    if not settings.llm_ready:
        state.log("提示：未配置 SILICONFLOW_API_KEY —— 检索/入库/成本/DOE 可运行，判断类步骤（筛选、机理、材料卡、定性评估）会跳过或退化。", echo)
    for key in (stages or DISCOVERY):
        st = state.stage(key)
        if resume and st.get("status") == "done" and key not in ("report", "doe"):
            state.log(f"跳过已完成的 {state.STAGE_NAMES[key]}（--no-resume 可强制重跑）", echo)
            results[key] = st
            continue
        stage_kw = {k: v for k, v in kw.items() if k in _STAGE_ARGS.get(key, ())}
        results[key] = run_stage(key, echo, **stage_kw)
    return results


_STAGE_ARGS = {
    "collect": ("topics", "max_hits_per_query", "max_keep", "make_cards", "use_llm_queries"),
    "mechanism": ("supplement",),
    "scout": ("codes", "do_prices", "do_cards", "discover"),
    "design": ("n_generated", "top_n", "seed"),
    "learn": ("data_path",),
    "scan": ("n_exploit", "n_explore", "p_min"),
    "review": ("round_no",),
}


def status_text() -> str:
    st = state.load()
    lines = ["阶段状态："]
    for key, name in state.STAGES:
        s = st["stages"].get(key, {})
        flag = {"done": "✔", "running": "…", "failed": "✘"}.get(s.get("status"), "·")
        extra = ""
        if s.get("outputs"):
            extra = "  → " + ", ".join(Path(p).name for p in s["outputs"])
        if s.get("error"):
            extra += f"  错误：{s['error']}"
        lines.append(f"  {flag} {name}{extra}")
    if st.get("rounds"):
        lines.append("轮次：" + "; ".join(f"第{r['round']}轮 最优过关成本 {r.get('best_pass_cost')}" for r in st["rounds"]))
        lines.append("收敛：" + str(stage_experiment.convergence_check(st["rounds"])))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="polysage.pipeline", description="膜方 AI 流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="发现阶段 ①～⑥")
    r.add_argument("--stages", default="", help="逗号分隔：collect,mechanism,scout,design,report,doe")
    r.add_argument("--no-resume", action="store_true")
    r.add_argument("--max-hits", type=int, default=None)
    r.add_argument("--max-keep", type=int, default=160)
    r.add_argument("--no-prices", action="store_true")
    r.add_argument("--topics", default="", help="只跑某些主题 key")
    l = sub.add_parser("learn", help="⑦ 回灌建模")
    l.add_argument("--data", default=None)
    s = sub.add_parser("scan", help="⑧ 扫描推荐")
    s.add_argument("--p-min", type=float, default=0.8)
    v = sub.add_parser("review", help="⑨ 复盘")
    v.add_argument("--round", type=int, default=1)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        stages = [x for x in a.stages.split(",") if x] or None
        run_discovery(stages, resume=not a.no_resume, max_hits_per_query=a.max_hits, max_keep=a.max_keep,
                      do_prices=not a.no_prices, topics=[x for x in a.topics.split(",") if x] or None)
    elif a.cmd == "learn":
        run_stage("learn", data_path=Path(a.data) if a.data else None)
        print(stage_experiment.record_round(len(state.load().get("rounds", [])) + 1))
    elif a.cmd == "scan":
        run_stage("scan", p_min=a.p_min)
    elif a.cmd == "review":
        run_stage("review", round_no=a.round)
    print(status_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
