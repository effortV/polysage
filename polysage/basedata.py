"""base（现用膜实测）：结构化录入、过关线派生、给 LLM/报告的文本。

文件：knowledge/00_项目/base.yaml（主）与 base.md（人类可读镜像）。
四项红线 = 4 个数：拉伸强度、撕裂强度、穿刺力、热封强度（不分 MD/TD）。
"""
from __future__ import annotations

from typing import Any

from . import db, kb
from .config import KB

BASE_PATH = KB["project"] / "base.yaml"

ITEMS: list[tuple[str, str, str]] = [
    # key, 中文, 单位
    ("tensile", "拉伸强度", "MPa"),
    ("tear", "撕裂强度", "N"),
    ("puncture", "穿刺力", "N"),
    ("seal", "热封强度", "N/15mm"),
]
LABEL = {k: n for k, n, _ in ITEMS}
UNIT = {k: u for k, _, u in ITEMS}
REQUIRED = [k for k, _, _ in ITEMS]
HIGHER_BETTER = {k: True for k, _, _ in ITEMS}


def load() -> dict[str, Any]:
    return kb.read_yaml(BASE_PATH) or {}


def save(data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    data["updated_at"] = db.now()
    kb.write_yaml(BASE_PATH, data)
    kb.write_text(kb.PROJECT_FILES["base"], to_markdown(data))
    return data


def set_items(items: dict[str, dict[str, Any]], *, thickness_um: float | None = None, note: str = "",
              seal_temp: float | None = None, methods: dict[str, str] | None = None) -> dict[str, Any]:
    """items: {key: {mean, sd?, n?}}；只更新给到的项，其余保留。"""
    cur = load()
    cur_items = cur.get("items", {}) or {}
    for k, v in items.items():
        if k not in LABEL:
            continue
        if v is None or v.get("mean") in (None, ""):
            cur_items.pop(k, None)
            continue
        entry = {"mean": float(v["mean"]), "unit": UNIT[k]}
        if v.get("sd") not in (None, ""):
            entry["sd"] = float(v["sd"])
        if v.get("n") not in (None, ""):
            entry["n"] = int(v["n"])
        if v.get("method"):
            entry["method"] = str(v["method"])
        cur_items[k] = entry
    cur["items"] = cur_items
    if thickness_um is not None:
        cur["thickness_um"] = float(thickness_um)
    if seal_temp is not None:
        cur["seal_temp"] = float(seal_temp)
    if note:
        cur["note"] = note
    if methods:
        cur.setdefault("methods", {}).update(methods)
    return save(cur)


def is_ready(data: dict[str, Any] | None = None) -> bool:
    d = data if data is not None else load()
    items = d.get("items", {}) or {}
    return all(k in items for k in REQUIRED)


def missing(data: dict[str, Any] | None = None) -> list[str]:
    d = data if data is not None else load()
    items = d.get("items", {}) or {}
    return [LABEL[k] for k in REQUIRED if k not in items]


def stats() -> dict[str, dict[str, float]]:
    """与 ml.dataset.base_stats 同构：{key: {mean, sd}}。"""
    d = load()
    out = {}
    for k, v in (d.get("items", {}) or {}).items():
        out[k] = {"mean": float(v["mean"]), "sd": float(v.get("sd") or 0.0)}
    return out


def thresholds() -> dict[str, float]:
    """过关线：力学/热封 ≥ base×0.95 且 ≥ base−1SD → 取较大者；雾度 ≤ base+1；透光率 ≥ base−1。"""
    from .formulation import constraints as C

    rule = C.load().get("pass_rule", {})
    ratio = float(rule.get("mechanical_ratio", 0.95))
    k_sd = float(rule.get("mechanical_sd", 1))
    thr: dict[str, float] = {}
    for k, s in stats().items():
        # 没有 SD（或 SD=0）时只用 0.95 倍率，不能把“base−1SD”退化成 base 本身
        thr[k] = max(s["mean"] * ratio, s["mean"] - k_sd * s["sd"]) if s.get("sd") else s["mean"] * ratio
    return thr


def judge(values: dict[str, float]) -> dict[str, Any]:
    """按过关线判定一组实测/预测值：返回 {key: {value, threshold, pass, margin_pct}} 与 overall。"""
    thr = thresholds()
    out: dict[str, Any] = {"items": {}, "pass": None, "n_checked": 0}
    ok_all = True
    for k, t in thr.items():
        if k not in values or values[k] is None:
            continue
        v = float(values[k])
        hb = HIGHER_BETTER.get(k, True)
        ok = v >= t if hb else v <= t
        margin = ((v - t) / abs(t) * 100) if hb else ((t - v) / abs(t) * 100)
        out["items"][k] = {"value": v, "threshold": round(t, 3), "pass": bool(ok), "margin_pct": round(margin, 1)}
        out["n_checked"] += 1
        ok_all &= bool(ok)
    out["pass"] = ok_all if out["n_checked"] else None
    return out


def to_markdown(d: dict[str, Any] | None = None) -> str:
    d = d if d is not None else load()
    items = d.get("items", {}) or {}
    if not items:
        return ("# base（现用膜实测）\n\n尚未录入。需要 4 个数：拉伸强度、撕裂强度、穿刺力、热封强度（均值，最好带 SD 与条数）与膜厚。\n"
                "录入方式：“实验与模型 → base 现用膜”表单，或在对话里说“现用膜实测：拉伸 MD 32 MPa……”。\n")
    thr = thresholds()
    lines = [f"# base（现用膜实测，{d.get('updated_at', '')}）", ""]
    if d.get("thickness_um"):
        lines.append(f"- 膜厚：{d['thickness_um']:g} μm")
    if d.get("seal_temp"):
        lines.append(f"- 热封测试温度：{d['seal_temp']:g} ℃")
    if d.get("note"):
        lines.append(f"- 说明：{d['note']}")
    lines += ["", "| 项目 | base 均值 | SD | 条数 | 过关线 | 单位 |", "|---|---|---|---|---|---|"]
    for k, n, u in ITEMS:
        if k in items:
            v = items[k]
            lines.append(f"| {n} | {v['mean']:g} | {v.get('sd', '—')} | {v.get('n', '—')} | {thr.get(k, float('nan')):.3g} | {u} |")
    miss = missing(d)
    if miss:
        lines += ["", f"缺：{'、'.join(miss)}（补齐前不判过关）"]
    lines += ["", "过关规则：四项均 ≥ base×0.95 且 ≥ base−1SD（无 SD 时只用 0.95）；不能靠加厚补强。"]
    return "\n".join(lines) + "\n"


def brief() -> str:
    """给 LLM 的一行摘要。"""
    d = load()
    items = d.get("items", {}) or {}
    if not items:
        return "base 尚未录入（只能给定性预期，不判过关）"
    thr = thresholds()
    parts = [f"{LABEL[k]} {items[k]['mean']:g}{UNIT[k]}（过关线 ≥{thr[k]:.3g}）" for k in items if k in thr]
    head = f"膜厚 {d['thickness_um']:g} μm；" if d.get("thickness_um") else ""
    miss = missing(d)
    return head + "；".join(parts) + (f"；缺 {'、'.join(miss)}" if miss else "")
