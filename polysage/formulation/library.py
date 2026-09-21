"""配方库（05_配方库）：保存 / 更新 / 导出，含内部技术路线表 4-3 的首版 Top 20 作为种子。"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .. import db
from ..config import KB
from . import constraints as C
from . import cost as COST

SEED_TOP20: list[dict[str, Any]] = [
    # 编号, 结构, 配方, 四项预期, 优先级, 思路, 风险
    dict(code="F01", structure="mono", comps={"LL": 45, "LD": 10, "HD": 5, "R1": 39, "AD": 1}, effects="≈/↓ ↓ ↓ ≈", priority="B",
         rationale="最直接：再生料 25→39，LLDPE 60→45；PPA 保表面与光学", risks="中：撕裂/穿刺可能低于 base"),
    dict(code="F02", structure="mono", comps={"LL": 42, "LD": 8, "HD": 5, "R1": 42, "POE": 2, "AD": 1}, effects="≈ ≈ ≈ ≈", priority="A",
         rationale="F01 基础上 2% POE 补撕裂与穿刺；POE 低剂量对外观影响很小", risks="低～中"),
    dict(code="F03", structure="mono", comps={"LL": 30, "mLL": 12, "LD": 8, "HD": 5, "R1": 44, "AD": 1}, effects="≈ ≈ ↑ ↑", priority="A",
         rationale="12% mLLDPE 顶替 30 个点 C4 的韧性；mLLDPE 起封温度低", risks="中：挤出压力上升，需 PPA；LDPE 类 52% 膜泡稳定"),
    dict(code="F04", structure="mono", comps={"LL": 30, "mLL": 10, "LD": 10, "R1": 35, "RL": 14, "AD": 1}, effects="≈ ↑ ↑ ↑", priority="A",
         rationale="去掉 HDPE 保撕裂；再生线性料补韧性且价低；mLLDPE 保穿刺/热封", risks="中：再生线性料来源与品质（避免含 PIB）"),
    dict(code="F05", structure="mono", comps={"LL": 35, "mLL": 8, "LD": 5, "HD": 5, "R1": 30, "RL": 16, "AD": 1}, effects="≈ ≈ ≈/↑ ≈/↑", priority="B",
         rationale="F04 的保守版：保留 5% HDPE 维持挺度，mLLDPE 减至 8%", risks="中"),
    dict(code="F06", structure="mono", comps={"LL": 45, "LD": 5, "HD": 5, "R1": 30, "RL": 14, "AD": 1}, effects="≈ ≈ ≈/↓ ≈", priority="B",
         rationale="不用茂金属，只靠再生线性料替代部分 LLDPE，成本最低的单层方案之一", risks="中：穿刺与外观取决于 RL 品质"),
    dict(code="F07", structure="mono", comps={"LL": 25, "mLL8": 12, "LD": 10, "R1": 40, "RL": 12, "AD": 1}, effects="≈ ↑ ↑↑ ↑", priority="A",
         rationale="C8 茂金属落镖/穿刺效率最高，可把 C4 降到 25%", risks="中：C8 茂金属价格高、货源需确认"),
    dict(code="F08", structure="mono", comps={"LL": 35, "LD": 8, "HD": 5, "R1": 45, "EVA": 6, "AD": 1}, effects="≈/↓ ≈ ≈ ↑↑", priority="B",
         rationale="低 VA EVA 改善热封与柔韧，抵消高再生比例的影响", risks="中：EVA 略增粘连倾向，需开口剂配合"),
    dict(code="F09", structure="mono", comps={"LL": 42, "LD": 10, "HD": 3, "R1": 30, "R2": 12, "POE": 2, "AD": 1}, effects="≈ ≈ ≈ ≈", priority="C",
         rationale="引入 12% 二级再生料试探二级料的外观容忍度", risks="高：晶点"),
    dict(code="F10", structure="mono", comps={"LL": 38, "LL6": 12, "LD": 8, "HD": 5, "R1": 36, "AD": 1}, effects="≈ ≈ ≈/↑ ≈", priority="B",
         rationale="齐格勒 C6 LLDPE 作为茂金属的低价替代，韧性介于 C4 与 mLLDPE 之间", risks="低"),
    dict(code="F11", structure="mono", comps={"LL": 45, "LD": 12, "R1": 42, "AD": 1}, effects="≈/↓ ↑ ≈/↓ ≈", priority="A",
         rationale="去掉 HDPE、略增 LDPE：撕裂改善，用于补偿再生料上调", risks="低"),
    dict(code="F12", structure="mono", comps={"LL": 50, "MD": 10, "R1": 39, "AD": 1}, effects="↑ ↓ ≈ ≈/↓", priority="C",
         rationale="MDPE 替代 LDPE+HDPE，提高挺度、降低成本", risks="中～高：撕裂下降、外观变化"),
    dict(code="F16", structure="ABA", comps={"LL": 25, "mLL": 16, "LD": 7.2, "R1": 15, "R2": 36, "AD": 0.8}, effects="≈ ≈/↑ ↑ ↑", priority="A（有三层设备时）",
         rationale="表层 mLLDPE+LLDPE+LDPE 保四项性能；芯层二级再生料 60% 降本（层比 20/60/20）", risks="前提：三层设备"),
    dict(code="F17", structure="ABA", comps={"LL": 36, "LD": 15.2, "HD": 6, "R1": 24, "RL": 18, "AD": 0.8}, effects="≈ ≈ ≈ ≈", priority="B（有三层设备时）",
         rationale="不用茂金属的三层方案：表层全新料，芯层再生料 70%", risks="前提：三层设备"),
    dict(code="F19", structure="mono", comps={"LL": 40, "mLL": 6, "LD": 10, "HD": 4, "R1": 39, "AD": 1}, effects="≈ ≈ ≈/↑ ≈/↑", priority="B",
         rationale="温和版：6% 茂金属 + 再生料 39%，改动小、风险低", risks="低"),
    dict(code="F20", structure="mono", comps={"LL": 28, "mLL": 12, "LD": 6, "R1": 40, "RL": 10, "EVA": 3, "AD": 1}, effects="≈ ≈ ↑ ↑↑", priority="A",
         rationale="茂金属 + 少量 EVA 双重保热封，再生料合计 50%", risks="中"),
]


def save_formulation(code: str, components: dict[str, float], *, structure: str = "mono", transparent: str | None = None,
                     predicted: dict[str, Any] | None = None, rationale: str = "", risks: str = "",
                     priority: str = "", status: str = "候选", origin: str = "") -> int:
    res = COST.compute(components)
    data = {
        "structure": structure, "transparent": transparent, "components_json": db.dumps(components),
        "cost_estimate": res.cost_estimate, "cost_actual": res.cost_actual, "cost_used": res.cost_used, "density": res.density,
        "area_index": res.area_index, "predicted_json": db.dumps(predicted or {}), "rationale": rationale,
        "risks": risks, "priority": priority, "status": status, "origin": origin, "updated_at": db.now(),
    }
    existing = db.q1("SELECT id FROM formulations WHERE code=?", (code,))
    if existing:
        db.update("formulations", existing["id"], data)
        fid = existing["id"]
    else:
        fid = db.insert("formulations", {"code": code, "created_at": db.now(), **data})
    export_library()
    return fid


def next_code(prefix: str = "G") -> str:
    rows = db.q("SELECT code FROM formulations WHERE code LIKE ?", (prefix + "%",))
    nums = [int(r["code"][len(prefix):]) for r in rows if r["code"][len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:02d}"


def list_formulations(status: str | None = None) -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM formulations" + (" WHERE status=?" if status else "") + " ORDER BY id",
                (status,) if status else ())
    for r in rows:
        r["components"] = db.loads(r["components_json"], {})
        r["predicted"] = db.loads(r.get("predicted_json"), {})
        r["formula"] = COST.format_components(r["components"])
        if r.get("cost_used") is None:
            r["cost_used"] = r["cost_actual"] if r.get("cost_actual") is not None else r.get("cost_estimate")
        if r.get("cost_actual") is not None:
            r["cost_tier"] = "实价"
        elif r.get("cost_used") is not None and r.get("cost_estimate") is not None and abs(r["cost_used"] - r["cost_estimate"]) > 0.5:
            r["cost_tier"] = "混合"
        else:
            r["cost_tier"] = "估计价"
    return rows


def set_status(code: str, status: str) -> None:
    r = db.q1("SELECT id FROM formulations WHERE code=?", (code,))
    if r:
        db.update("formulations", r["id"], {"status": status, "updated_at": db.now()})
        export_library()


def recompute_costs() -> int:
    """价格回填后重算全部配方成本。"""
    n = 0
    for r in list_formulations():
        res = COST.compute(r["components"])
        db.update("formulations", r["id"], {"cost_estimate": res.cost_estimate, "cost_actual": res.cost_actual,
                                            "cost_used": res.cost_used, "density": res.density, "area_index": res.area_index,
                                            "updated_at": db.now()})
        n += 1
    export_library()
    return n


SEED_ORIGIN = "内部技术路线 V2.0 表 4-3 首版"


def seed_top20() -> int:
    """（已停用种子）配方库只收研发流水线、对话/表单推荐和手工录入的配方；这里顺手把早期写入的首版种子清掉。返回清掉的条数。"""
    return purge_seed_top20()


def purge_seed_top20() -> int:
    rows = db.q("SELECT id FROM formulations WHERE origin=?", (SEED_ORIGIN,))
    for r in rows:
        db.delete("formulations", r["id"])
    if rows:
        export_library()
    return len(rows)


def find_by_components(components: dict[str, float]) -> dict[str, Any] | None:
    """按组分（忽略顺序）找已有配方，避免同一配方多个编号。"""
    key = db.dumps({k: float(v) for k, v in sorted(components.items()) if v})
    for f in list_formulations():
        if db.dumps({k: float(v) for k, v in sorted(f["components"].items()) if v}) == key:
            return f
    return None


ORIGIN_LABELS = {"pipeline": "研发流水线 ④", "chat": "对话推荐", "form": "配方推荐页", "manual": "手工录入", "experiment": "实验回灌"}


def export_library(path: Path | None = None) -> Path:
    path = path or KB["formulation"] / "配方库.csv"
    c = C.load()
    base_cost = COST.compute_simple(C.base_formulation(c))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "结构", "配方(重量%)", "当前成本(混合口径)", "口径", "估计价成本", "实价成本", "降本(元/吨)", "面积成本指数",
                    "四项预期", "优先级", "状态", "设计思路", "风险", "来源", "更新时间"])
        for r in list_formulations():
            cost = r["cost_used"]
            sav = round(base_cost - cost) if (base_cost and cost) else ""
            w.writerow([r["code"], r["structure"], r["formula"],
                        round(cost) if cost else "", r["cost_tier"],
                        round(r["cost_estimate"]) if r["cost_estimate"] else "",
                        round(r["cost_actual"]) if r["cost_actual"] else "", sav,
                        r["area_index"], (r["predicted"] or {}).get("effects_short", ""), r["priority"], r["status"],
                        r["rationale"], r["risks"], r["origin"], r["updated_at"]])
    return path
