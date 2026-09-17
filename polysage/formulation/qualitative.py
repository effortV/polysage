"""定性评估：LLM 依据材料卡 / 文献卡对候选配方给出四项性能的方向与幅度、依据、风险与过关把握。

排序规则（内部技术路线 4.3）：按“降本额 × 过关把握”排序取前 20。
"""
from __future__ import annotations

import re
from typing import Any

from .. import kb, llm
from . import constraints as C
from .generator import Candidate

INDICATORS = ["拉伸", "撕裂", "穿刺", "热封"]
CONFIDENCE_WEIGHT = {"高": 1.0, "中": 0.6, "低": 0.3}
_DIR_RE = re.compile(r"↑↑|↓↓|≈/↑|≈/↓|↑|↓|≈|持平|略升|略降|上升|下降|提升|降低")
_DIR_MAP = {"持平": "≈", "略升": "↑", "略降": "↓", "上升": "↑", "下降": "↓", "提升": "↑", "降低": "↓"}


def direction(text: Any) -> str:
    """从 LLM 的“方向 + 理由”文本里抽出方向符号（↑↑/↑/≈/↓/↓↓）。"""
    m = _DIR_RE.search(str(text or ""))
    if not m:
        return "?"
    return _DIR_MAP.get(m.group(0), m.group(0))

SYSTEM = (
    "你是包装膜配方设计师。项目目标：四项性能（拉伸强度、撕裂强度、穿刺力、热封强度）全部 ≥ base 的前提下降低原料成本"
    "（现用 LLDPE 约 12,000 元/吨是成本大头；候选思路：同类更便宜的 LLDPE 直接替代、再生料上调、高效韧性树脂低比例替代、HDPE 减量）。"
    "规则：① 只做定性判断（方向 ↑↑/↑/≈/↓/↓↓ + 幅度说明），不给虚构的精确数值；② 每个判断标依据（材料卡名称 / 文献卡编号 / TDS / 经验判断）；"
    "③ 不确定就写“待验证”；④ 不改 base、约束、价格；⑤ 同类配方判断要一致：单层膜再生料合计 ≥ 50% 时，穿刺/撕裂默认 ↓、过关把握不高于“中”，"
    "除非材料卡/文献卡有相反证据；用同类更便宜的 LLDPE 1:1 替代且其余不变时，四项默认 ≈、把握“高（若替代牌号 TDS 与现用相当）”。输出严格 JSON。"
)

PROMPT = (
    "当前 base 配方：{base}\n约束摘要：{constraints}\n\n材料卡摘要：\n{materials}\n\n文献/专利卡摘要：\n{cards}\n\n"
    "请逐个评估下面的候选配方，输出 JSON：{{\"items\": [{{\"index\": 序号, \"effects\": {{\"拉伸\": \"↑/≈/↓ + 一句理由\", \"撕裂\": ..., \"穿刺\": ..., \"热封\": ...}}, "
    "\"expected\": \"预期影响一句话（如：拉伸/穿刺持平，热封略升）\", \"risks\": \"风险点（外观/加工/货源）\", \"risk_level\": \"低/中/高\", "
    "\"pass_confidence\": \"高/中/低（四项均 ≥ base 的把握）\", \"evidence\": \"依据：材料卡/文献卡/经验判断\"}}]}}\n\n"
    "候选配方：\n{candidates}"
)


def _constraints_brief(c: dict[str, Any]) -> str:
    bits = []
    for r in c.get("rules", []):
        rng = []
        if "min" in r:
            rng.append(f"≥{r['min']}")
        if "max" in r:
            rng.append(f"≤{r['max']}")
        bits.append(f"{r['name']} {'/'.join(rng)}%")
    bits.append(f"成本 < {c.get('cost_limit')} 目标 ≤ {c.get('cost_target')} 元/吨")
    return "；".join(bits)


def assess(cands: list[Candidate], batch: int = 8) -> list[dict[str, Any]]:
    """返回与 cands 对齐的评估字典列表；LLM 未配置时返回占位。批次并行调用。"""
    from concurrent.futures import ThreadPoolExecutor

    from .. import basedata

    c = C.load()
    base = ", ".join(f"{k} {v:g}%" for k, v in C.base_formulation(c).items()) + "；现用膜实测 base：" + basedata.brief()
    mats = kb.cards_digest("material") or "（尚无材料卡；请先在“知识库”页生成，或按经验判断并标注）"
    cards = (kb.cards_digest("literature", limit=30, max_chars=3000) + "\n" +
             kb.cards_digest("patent", limit=15, max_chars=1500)).strip() or "（尚无文献/专利卡）"
    out: list[dict[str, Any]] = [{} for _ in cands]
    if not llm.settings.llm_ready:
        return [{"expected": "待评估（未配置 LLM）", "risks": "", "risk_level": "", "pass_confidence": "中",
                 "effects": {}, "evidence": ""} for _ in cands]

    def _one(i: int) -> tuple[int, list[dict[str, Any]]]:
        part = cands[i:i + batch]
        listing = "\n".join(
            f"{i + j + 1}. [{cd.theme}] {cd.text()}｜成本 {cd.cost}（{cd.cost_tier}）｜较 base 降 {cd.savings_pct:.1f}%"
            if cd.savings_pct is not None else f"{i + j + 1}. [{cd.theme}] {cd.text()}｜成本 {cd.cost}"
            for j, cd in enumerate(part)
        )
        msg = PROMPT.format(base=base, constraints=_constraints_brief(c), materials=mats, cards=cards, candidates=listing)
        try:
            data = llm.chat_json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}], max_tokens=4000, temperature=0.0)
        except llm.LLMNotConfigured:
            return i, []
        except llm.LLMError as e:
            # 单批超时/出错：该批给占位，其余批次照常
            return i, [{"index": i + j + 1, "expected": f"（评估失败：{str(e)[:60]}）", "pass_confidence": "中", "effects": {},
                        "risks": "", "risk_level": "", "evidence": ""} for j in range(len(part))]
        items = data.get("items", []) if isinstance(data, dict) else data
        return i, list(items or [])

    starts = list(range(0, len(cands), batch))
    # LLM 调用是 I/O 等待：4 路并发把 7 批的耗时压到约 1/4
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(starts)))) as ex:
        for i, items in ex.map(_one, starts):
            for it in items:
                try:
                    idx = int(it.get("index")) - 1
                except (TypeError, ValueError):
                    continue
                if i <= idx < i + batch and idx < len(cands):
                    out[idx] = it
    return out


def rank(cands: list[Candidate], assessments: list[dict[str, Any]], top_n: int = 20) -> list[dict[str, Any]]:
    rows = []
    for cd, a in zip(cands, assessments):
        conf = str(a.get("pass_confidence", "中")).strip()[:1]
        w = CONFIDENCE_WEIGHT.get(conf, 0.6)
        score = (cd.savings or 0) * w
        eff = a.get("effects") or {}
        rows.append({
            "theme": cd.theme, "components": cd.components, "formula": cd.text(), "cost": cd.cost,
            "cost_tier": cd.cost_tier, "savings": cd.savings, "savings_pct": cd.savings_pct, "density": cd.density,
            "area_index": cd.area_index, "expected": a.get("expected", ""), "risks": a.get("risks", ""),
            "risk_level": a.get("risk_level", ""), "pass_confidence": a.get("pass_confidence", ""),
            "evidence": a.get("evidence", ""),
            "effects_short": " ".join(direction(eff.get(k)) for k in INDICATORS) if eff else "",
            "effects": eff, "score": score,
        })
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows[:top_n], 1):
        r["rank"] = i
    return rows[:top_n]
