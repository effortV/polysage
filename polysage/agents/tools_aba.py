"""ABA 三层膜的对话工具：分档最低成本、评估一个配方、价格场景、实验闭环、系数表。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .tools import tool

_SCEN = {"type": "string", "enum": ["current", "live", "fut1", "fut2", "custom"],
         "description": "价格：current 价格卡当前价；live 按 L 期货修正到今天；fut1/fut2 近月/次近月期货；custom 自定义 LLDPE 涨跌"}
_LAYER = {"type": "object", "additionalProperties": {"type": "number"}, "description": "该层组分重量百分比，合计 100，如 {\"LL\": 60, \"R1\": 30, \"FL\": 10}"}


def _req(v: Any) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    return None if v <= 0 else v


@tool("aba_optimize",
      "ABA 三层膜分档寻优：在给定的料里，按透光三档（0 一般/1 好/2 很好）分别找元/吨最低、纵横拉断力满足要求的方案"
      "（自动调 B 层填充、B 层 HDPE、再生比例、A 层占比、厚度），并给出“只用现有料”和“加/换新料”两组。结果自动入配方库并导出 Word。",
      {"md_req": {"type": "number", "description": "纵向拉断力至少是现配方的几倍：1 = 不降，1.1 = 提升 10%，0.95 = 允许降 5%，0 = 不管"},
       "td_req": {"type": "number", "description": "横向拉断力，含义同上"},
       "grade_min": {"type": "integer", "description": "透光至少几档：0/1/2；传 -1（默认）= 三档分别给出最低成本方案"},
       "t_min": {"type": "number", "description": "最薄厚度（丝），默认 3"},
       "t_max": {"type": "number", "description": "最厚厚度（丝），默认 6；要固定厚度就让 t_min = t_max"},
       "t_ref": {"type": "number", "description": "现配方的对照厚度（丝），默认 4.5"},
       "ref_filler": {"type": "number", "description": "现配方 B 层本来加了多少填充 %（默认 0）"},
       "ref_hdpe": {"type": "number", "description": "现配方 B 层本来加了多少 HDPE %（默认 0）"},
       "scenario": _SCEN, "custom_pct": {"type": "number", "description": "scenario=custom 时 LLDPE 涨跌百分比，如 5 或 -3"},
       "use_new": {"type": "boolean", "description": "是否允许加/换库里其它买得到的料（默认 true）"}}, [])
def aba_optimize(md_req: float = 1.0, td_req: float = 1.0, grade_min: int = -1, t_min: float = 3.0, t_max: float = 6.0,
                 t_ref: float = 4.5, ref_filler: float = 0.0, ref_hdpe: float = 0.0, scenario: str = "current",
                 custom_pct: float = 0.0, use_new: bool = True):
    from ..aba import optimize as O
    from ..aba import report as RP
    from .. import procurement

    q = O.Query(md_req=_req(md_req), td_req=_req(td_req), grade_min=None if grade_min is None or int(grade_min) < 0 else int(grade_min),
                t_min=float(t_min), t_max=float(t_max), t_ref=float(t_ref), ref_filler=float(ref_filler), ref_hdpe=float(ref_hdpe),
                scenario=scenario or "current", custom_pct=float(custom_pct or 0), use_new=bool(use_new))
    out = O.run(q)
    text = O.to_text(out)
    path = RP.export_docx(out)                    # 成文输出一律 Word
    codes = RP.save_to_library(out, origin="对话·分档")
    best = None
    for g in sorted((out["groups"].get("现有料") or {}), reverse=False):
        items = out["groups"]["现有料"][g]
        if items:
            best = items[0]
            break
    extra = [f"导出 Word：05_配方库/{path.name}（回答里写上这个文件名，用户就能直接下载）", f"已入配方库：{'、'.join(codes) or '无'}"]
    if best:
        extra.append(procurement.text(RP.procurement_for(best)))
        if out["scenario"]["name"] != "current":
            extra.append(f"注意：上面的进货清单按价格卡上的到厂价合计；按“{out['scenario']['label']}”算，这个方案是 {best['cost']:.0f} 元/吨。"
                         "回答里两个数都要说清是哪个价格口径，别混在一起。")
    return text + "\n\n" + "\n".join(extra), []


@tool("aba_evaluate", "评估一个指定的 ABA 配方：元/吨、克重、相对现配方的纵横拉断力、预测透光档、依据与约束检查。",
      {"a_layer": _LAYER, "b_layer": _LAYER, "a_ratio": {"type": "number", "description": "A 层占整膜比例，如 0.45"},
       "thickness": {"type": "number", "description": "厚度（丝）"}, "t_ref": {"type": "number", "description": "现配方对照厚度，默认 4.5"},
       "scenario": _SCEN, "custom_pct": {"type": "number"}}, ["a_layer", "b_layer"])
def aba_evaluate(a_layer: dict, b_layer: dict, a_ratio: float = 0.45, thickness: float = 4.5, t_ref: float = 4.5,
                 scenario: str = "current", custom_pct: float = 0.0):
    from ..aba import prices as P
    from ..aba.model import Ctx, Design, base_design
    from ..aba.optimize import saving_text

    scen = P.build(scenario or "current", float(custom_pct or 0))
    ctx = Ctx(prices=scen["prices"])
    d = Design(a={k: float(v) for k, v in a_layer.items()}, b={k: float(v) for k, v in b_layer.items()},
               a_ratio=float(a_ratio), thickness=float(thickness))
    ref = base_design(float(t_ref))
    ev, r0 = ctx.evaluate(d, ref), ctx.evaluate(ref, ref)
    pct = None if not (ev["cost"] and r0["cost"]) else (r0["cost"] - ev["cost"]) / r0["cost"] * 100
    lines = [f"{d.text()}（价格：{scen['label']}）",
             f"成本 {ev['cost'] if ev['cost'] is not None else '缺价'} 元/吨（{saving_text(pct)}）｜克重 {ev['gsm']} g/m²",
             f"纵向拉断力 {ev['md_rel']:.2f}× 现配方｜横向 {ev['td_rel']:.2f}×｜透光预测 {ev['grade_label']}（分 {ev['trans_score']}）｜把握 {ev['confidence']}"
             + (f"｜约 纵 {ev['md_N']} N / 横 {ev.get('td_N')} N" if ev.get("md_N") else "")]
    lines += [f"· {x}" for x in ctx.drivers(d, ref, top=6)]
    if ev["issues"]:
        lines.append("约束问题：" + "；".join(ev["issues"]))
    if ev["missing"]:
        lines.append("缺价格：" + "、".join(ev["missing"]))
    lines += [f"· {n}" for n in scen["notes"]]
    return "\n".join(lines), []


@tool("aba_prices", "看主要原料在几种价格场景下的价格：当前价格卡、按期货修正到今天、近月/次近月期货。", {})
def aba_prices():
    from ..aba import prices as P

    codes = ["LL", "R1", "FL", "HD", "LD", "mLL"]
    rows, notes = [], []
    scens = [P.build(n) for n in ("current", "live", "fut1", "fut2")]
    for s in scens:
        notes += [n for n in s["notes"] if n not in notes]
    head = "代码 | " + " | ".join(s["label"] for s in scens)
    for c in codes:
        if c in scens[0]["prices"]:
            rows.append(f"{c} | " + " | ".join(f"{s['prices'].get(c, '-')}" for s in scens))
    ref = scens[1].get("ref") or {}
    tail = [f"L 主连 {ref.get('L_now')}（{ref.get('L_now_date')}），价格卡报价日 {ref.get('LL_date')} 时为 {ref.get('L_then')}"] if ref.get("L_now") else []
    return "\n".join([head] + rows + tail + [f"· {n}" for n in notes]), []


@tool("aba_experiment", "实验闭环：round 在后台跑一整轮（更新行情→找新料→分档寻优→出 n 组试验方案，另出《试验方案》《运行报告》两份 Word，"
      "约 5~15 分钟，进度用 background_status 查）；first 立刻出 n 组试验方案（默认 50，最多 150：现配方对照 + 各档预测最省 + 新料 + "
      "透光拿不准的 + D-最优建模组，含 Word）；有回填数据时 round/first 都会先自动校准，再按当天的价格挑，建模组避开做过的配方；"
      "replace=true 时作废还没回填的最近一轮、用新方案替换（用户说“把第 1 轮换成 80 组”就这样用）；"
      "next 小步推荐 10 组左右；calibrate 用已回填的实测校准系数；status 看试验和校准状态。"
      "回填实测：把方案 Word 里的数据记录表填好，在「实验与校准」页上传。回答里写上生成的文件名，用户就能直接下载。",
      {"action": {"type": "string", "enum": ["round", "first", "next", "calibrate", "status"]},
       "n": {"type": "integer", "description": "这一轮几组（12~150，默认 50）"},
       "replace": {"type": "boolean", "description": "最近一轮还没回填时作废它、用这次的方案替换（不另开一轮）"},
       "md_req": {"type": "number"}, "td_req": {"type": "number"}, "t_ref": {"type": "number"}}, ["action"])
def aba_experiment(action: str, n: int = 50, md_req: float = 1.0, td_req: float = 1.0, t_ref: float = 4.5,
                   replace: bool = False):
    from ..aba import experiments as E
    from ..aba import rules as R

    n = max(12, min(int(n or 50), E.MAX_ROUND))
    if action == "round":
        from .. import jobs
        from ..aba import campaign
        from .tools_ops import JOB_LABELS

        running = jobs.info()
        if running:
            return f"后台正在跑「{JOB_LABELS.get(running['name'], running['name'])}」，等它跑完再开始。", []
        ok = jobs.start("aba_round", lambda: campaign.run(n, t_ref=float(t_ref), replace=bool(replace)))
        return ("已在后台开始跑一整轮：更新行情 → 找新料 → 分档寻优 → 出 %d 组试验方案和两份 Word，约 5~15 分钟；"
                "跑完在网页「实验与校准 › 各轮结果」看运行报告、每一组和下载 Word，或再问我进度。" % n if ok else "没能启动后台任务，请稍后再试。"), []
    if action == "first":
        from ..aba import campaign

        r = E.plan_round(n, t_ref=float(t_ref), replace=bool(replace))
        campaign.report_docx(r["round"], t_ref=float(t_ref))
        lines = [f"第 {r['round']} 轮试验方案 {len(r['trials'])} 组（" + "，".join(f"{k} {v}" for k, v in r["meta"]["counts"].items())
                 + f"）。Word：06_实验数据/{Path(r['docx']).name}；表格：06_实验数据/{Path(r['xlsx']).name}；"
                 "网页「实验与校准 › 各轮结果」也能看和下载"]
        lines += [f"{t['code']}｜{t['purpose']}｜{t['design'].text()}｜预计 {t['predicted'].get('cost')} 元/吨，"
                  f"透光 {t['predicted'].get('grade_label')}" for t in r["trials"]]
        return chr(10).join(lines), []
    if action == "next":
        r = E.next_round({"md_req": _req(md_req), "td_req": _req(td_req), "t_ref": float(t_ref)})
        lines = [f"第 {r['round']} 轮推荐 {len(r['trials'])} 组。Word：06_实验数据/{Path(r['docx']).name}；"
                 "网页「实验与校准 › 各轮结果」也能看和下载"]
        lines += [f"{t['code']}｜{t['purpose']}｜{t['design'].text()}｜预计 {t['predicted'].get('cost')} 元/吨，透光 {t['predicted'].get('grade_label')}"
                  for t in r["trials"]]
        return "\n".join(lines), []
    if action == "calibrate":
        r = E.calibrate(t_ref=float(t_ref))
        if not r.get("ok"):
            return r.get("reason", "还不能校准"), []
        return (f"已用 {r['n']} 组实测校准：现配方 {t_ref:g} 丝 纵向约 {r.get('md_N_ref')} N、横向约 {r.get('td_N_ref')} N；"
                f"拟合误差 纵 {r.get('md_rmse_pct')}% 横 {r.get('td_rmse_pct')}%；透光档命中 {r.get('grade_hit')}%。"
                f"校准后的系数：{r.get('roles')}"), []
    ts = E.trials()
    m = E.measured()
    cal = R.calibration()
    return (f"试验共 {len(ts)} 组（{len({t['round'] for t in ts})} 轮），已回填 {len(m)} 组；"
            + (f"最近一次校准 {cal.get('date')}，用了 {cal.get('n')} 组。" if cal else "还没校准（系数全是先验）。")), []


@tool("aba_coefficients", "看 ABA 定性模型的系数表：每类料每 10 个百分点对纵向/横向拉伸、透光的影响，可信度与依据，是否已被实验校准。", {})
def aba_coefficients():
    from ..aba import rules as R

    t = R.load_table()
    lines = ["角色 | 纵向/10点 | 横向/10点 | 透光分/10点 | 只能放 | 层内上限 | 可信度 | 依据"]
    for k, v in t.items():
        lines.append(f"{v['label']} | {v['md']:+.3f} | {v['td']:+.3f} | {v['trans']:+.2f} | {v['layers']} 层 | {v['cap']}% | "
                     f"{'已校准' if v.get('calibrated') else v['conf']} | {v['basis']}")
    lines.append(f"透光分 ≥{R.GRADE_CUTS[1]} 很好、≥{R.GRADE_CUTS[0]} 好、其余一般；每厚 1 丝透光分 {R.THICK_TRANS:+.2f}；拉断力与厚度成正比。")
    return "\n".join(lines), []
