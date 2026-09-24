"""命令行：推荐智能体。

  python -m polysage.recommend example > input.json     # 输出示例输入
  python -m polysage.recommend run input.json           # 按输入出方案（材料/价格会写入库）
  python -m polysage.recommend run                      # 用原料库现有材料与价格出方案
  python -m polysage.recommend screen [--ref LL]        # 只做替代品窗口筛选
  python -m polysage.recommend price LLC 8200 --type actual --source "供应商A 2026-09-16"   # 登记价格并联动重排
  python -m polysage.recommend refresh                  # 网查参考价刷新（需 LLM）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from . import pricing, recommender
from .formulation import screening as S


def _print(s: str) -> None:
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("utf-8", errors="replace").decode("utf-8"), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="polysage.recommend", description="膜方 AI 推荐智能体")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("example")
    r = sub.add_parser("run")
    r.add_argument("input", nargs="?", default=None)
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--n", type=int, default=20)
    s = sub.add_parser("screen")
    s.add_argument("--ref", default="LL")
    p = sub.add_parser("price")
    p.add_argument("code")
    p.add_argument("price", type=float)
    p.add_argument("--type", default="estimate", choices=["estimate", "actual"])
    p.add_argument("--source", default="命令行登记")
    sub.add_parser("refresh")
    a = ap.parse_args(argv)

    if a.cmd == "example":
        _print(json.dumps(recommender.example_input(), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "run":
        inp = json.loads(Path(a.input).read_text(encoding="utf-8")) if a.input else {}
        inp.setdefault("n_schemes", a.n)
        if a.no_llm:
            inp["use_llm"] = False
        out = recommender.recommend(inp)
        _print(f"模式：{out['mode']}｜现配方成本 {pricing.cost_text(out['base_cost'])}｜上限 {out['cost_limit']}")
        for n in out["notes"]:
            _print("· " + n)
        df = pd.DataFrame([{"排名": x["rank"], "配方": x["formula"], "成本": x["cost"], "降本%": round(x["savings_pct"] or 0, 1),
                            "四项": x.get("effects_short"), "把握": x.get("pass_confidence"),
                            "P(过关)": round((x.get("ml") or {}).get("p_pass", float("nan")), 2) if x.get("ml") else None} for x in out["schemes"]])
        _print(df.to_string(index=False))
        _print("产出：" + "; ".join(out["outputs"]))
        return 0
    if a.cmd == "screen":
        res = S.screen(reference=a.ref)
        _print(f"基准 {res['reference']['code']} {res['reference']['name']} 价格 {res['reference']['price']}；窗口 {res['windows']}")
        _print(pd.DataFrame(S.to_rows(res))[["分组", "代码", "材料", "价格(元/吨)", "较基准", "密度窗口", "熔点窗口", "MFR窗口", "物性匹配度", "结论"]].to_string(index=False))
        return 0
    if a.cmd == "price":
        res = pricing.record_price(a.code, a.price, a.type, source=a.source)
        _print(f"已登记；base 成本 {pricing.cost_text(res['base_cost'])}；{res['n_flagged']} 个配方排名/成本明显变化；报告：{pricing.REPORT_PATH}")
        return 0
    if a.cmd == "refresh":
        res = pricing.refresh_estimates(echo=_print)
        _print(f"检查 {res['n_checked']} 项，{len(res['changed_prices'])} 项变化；报告：{pricing.REPORT_PATH}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
