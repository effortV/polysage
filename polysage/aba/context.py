"""给对话智能体的固定上下文：现配方、在用料与价格、模型口径、校准状态。"""
from __future__ import annotations

from . import rules as R
from .model import DEFAULT_T_REF, Ctx, base_design
from .prices import price_row


def brief() -> str:
    ctx = Ctx()
    ref = base_design(DEFAULT_T_REF)
    ev = ctx.evaluate(ref, ref)
    lines = ["现配方（ABA 三层共挤）：A 层（两面表层合计）约 45%、B 层（芯层）约 55%，比例可调；A、B 共用基底 "
             "LLDPE 50 kg + 高压再生回料 25 kg（LL 66.7 / R1 33.3）；填充母料只加在 B 层，一般 10~50%；需要挺度时 B 层加约 10% HDPE。"
             f"按 {DEFAULT_T_REF:g} 丝、不加填充计：{ev['cost'] or '缺价'} 元/吨，预测透光 {ev['grade_label']}。"]
    prices = []
    for code, label in (("LL", "LLDPE"), ("R1", "高压再生回料"), ("FL", "填充母料"), ("HD", "HDPE"), ("LD", "LDPE")):
        r = price_row(code)
        if r:
            prices.append(f"{label}（{code}）{r['price']:.0f}（{'实价' if r['price_type'] == 'actual' else '估计价'}，{r['price_date'] or '日期不详'}）")
    lines.append("价格卡：" + "；".join(prices) + "。")
    lines.append("性能只看三项：纵向拉断力、横向拉断力（N，和厚度成正比）、透光度（0 一般 / 1 好 / 2 很好，人工对照标样判档）。"
                 "成本口径：元/吨（厚度不改变每吨价格，但影响拉断力和透光）。厚度可设 2~8 丝，常用 3~6 丝（1 丝 = 10 μm）。")
    cal = R.calibration()
    if cal:
        lines.append(f"模型已用 {cal.get('n')} 组实测校准（{cal.get('date')}）：现配方 {DEFAULT_T_REF:g} 丝约 纵 {cal.get('md_N_ref')} N / "
                     f"横 {cal.get('td_N_ref')} N。")
    else:
        lines.append("还没有实验数据：所有性能都是定性模型（系数表）的预测，必须写明“定性预测，需小试验证”；"
                     "现配方的拉断力也还没有绝对值，只能说“是现配方的多少倍”。")
    return "\n".join(lines)
