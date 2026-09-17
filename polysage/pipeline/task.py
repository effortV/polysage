"""任务书（00_项目/task.yaml）：产品、现配方、红线、成本上限。所有阶段只从这里读任务信息。"""
from __future__ import annotations

from typing import Any

from .. import kb
from ..config import KB

TASK_PATH = KB["project"] / "task.yaml"

DEFAULT_TASK: dict[str, Any] = {
    "product": {
        "name": "PE 包装膜",
        "structure": "mono",          # mono | ABA
        "thickness_um": None,           # 待甲方提供
        "process": "吹膜",
    },
    "current_formulation": {
        "LL": {"pct": 60, "grade": "C4 LLDPE（7042 类，待确认）"},
        "LD": {"pct": 10, "grade": "LDPE（2426H 类，待确认）"},
        "HD": {"pct": 5, "grade": "HDPE 膜料（5000S 类，待确认）"},
        "R1": {"pct": 25, "grade": "再生高压一级透明料"},
    },
    "formulation_note": "甲方口述：LLDPE 60%，LDPE+HDPE+再生一级料合计 40%；内部比例 10/5/25 为假设，待确认",
    "red_lines": ["拉伸强度", "撕裂强度", "穿刺力", "热封强度"],
    "cost": {"current": 8000, "limit": 8000, "target": 7600, "unit": "元/吨", "note": "甲方口述约 8000；正式以回填实价为准"},
    "targets": {"replace_lldpe_pct": 30, "note": "甲方希望替代约 30% LLDPE；接受其他 PE 牌号、调比例、少量其他材料"},
    "search": {"year_from": 2000, "max_hits_per_query": 15, "languages": ["zh", "en"]},
}


def load() -> dict[str, Any]:
    t = kb.read_yaml(TASK_PATH)
    if not t:
        kb.write_yaml(TASK_PATH, DEFAULT_TASK)
        return dict(DEFAULT_TASK)
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
    return (f"产品：{p['name']}，{p['process']}，{'单层' if p['structure'] == 'mono' else '三层 ABA'}，"
            f"膜厚 {p.get('thickness_um') or '待定'} μm。\n"
            f"现配方：{comps}。{t.get('formulation_note', '')}\n"
            f"红线：{'、'.join(t['red_lines'])} 全部 ≥ base。\n"
            f"成本：现约 {t['cost']['current']} {t['cost']['unit']}，上限 {t['cost']['limit']}，目标 ≤ {t['cost']['target']}。\n"
            f"甲方意愿：{t['targets'].get('note', '')}")
