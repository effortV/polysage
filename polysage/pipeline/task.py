"""任务书（00_项目/task.yaml）：产品、现配方、红线、成本上限。所有阶段只从这里读任务信息。"""
from __future__ import annotations

from typing import Any

from .. import kb
from ..config import KB

TASK_PATH = KB["project"] / "task.yaml"

TASK_VERSION = 2          # 2 = ABA 三层、三项指标（2026-11 甲方调整）

DEFAULT_TASK: dict[str, Any] = {
    "version": TASK_VERSION,
    "product": {
        "name": "PE 包装膜（ABA 三层共挤）",
        "structure": "ABA",
        "layers": "A 层（两面表层）约 45%、B 层（芯层）约 55%，比例可调",
        "thickness": "2~8 丝可调，常用 3~6 丝（1 丝 = 10 μm）；越厚越牢、透光越差",
        "thickness_um": None,
        "process": "吹膜",
    },
    "current_formulation": {
        "LL": {"pct": 66.7, "grade": "LLDPE（每 75 kg 基底里 50 kg）"},
        "R1": {"pct": 33.3, "grade": "高压再生回料，接近新料颜色，8000~8500 元/吨（每 75 kg 基底里 25 kg）"},
    },
    "formulation_note": ("A、B 两层共用上面的基底；填充母料（约 2500 元/吨）只加在 B 层，一般 10~50%；"
                         "需要挺度和硬度时 B 层加约 10% 低压 HDPE。以上是大致情况，不是精确配方。"),
    "red_lines": ["纵向拉断力（N）", "横向拉断力（N）", "透光度（0 一般 / 1 好 / 2 很好，人工对照标样判档）"],
    "cost": {"current": 9200, "limit": 9200, "target": 8500, "unit": "元/吨",
             "note": "按吨计价；现配方不加填充约 9200，随价格卡与期货变化"},
    "targets": {"note": ("三项性能满足要求（不降或提升）的前提下元/吨最低；按透光三档分别给最低成本方案；"
                         "可按当前价或期货价重算；找能提性能、降成本的新材料（文献/专利/网查，必须买得到）；"
                         "现在没有数据，先定性，之后按实验回填转数据驱动")},
    "search": {"year_from": 2000, "max_hits_per_query": 15, "languages": ["zh", "en"]},
}


def load() -> dict[str, Any]:
    t = kb.read_yaml(TASK_PATH)
    if not t or int(t.get("version") or 1) < TASK_VERSION:
        # 旧任务书是单层、四项指标（拉伸/撕裂/穿刺/热封）：按甲方 2026-11 的调整整体换掉，只保留检索设置
        new = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_TASK.items()}
        if t and isinstance(t.get("search"), dict):
            new["search"] = {**DEFAULT_TASK["search"], **t["search"]}
        kb.write_yaml(TASK_PATH, new)
        return new
    for k, v in DEFAULT_TASK.items():
        t.setdefault(k, v)
    return t


def save(t: dict[str, Any]) -> None:
    kb.write_yaml(TASK_PATH, t)


def brief(t: dict[str, Any] | None = None) -> str:
    """给 LLM 的任务书摘要。"""
    t = t or load()
    p = t["product"]
    comps = "、".join(f"{k} {v['pct']}%（{v.get('grade', '')}）" for k, v in t["current_formulation"].items())
    return (f"产品：{p['name']}，{p['process']}；{p.get('layers', '')}；厚度 {p.get('thickness', '待定')}。\n"
            f"现配方基底：{comps}。{t.get('formulation_note', '')}\n"
            f"性能指标：{'、'.join(t['red_lines'])}。\n"
            f"成本：{t['cost'].get('note', '')}（{t['cost']['unit']}）。\n"
            f"目标：{t['targets'].get('note', '')}")
