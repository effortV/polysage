"""推荐智能体核心：输入“目标膜 + 可用材料（物性 + 价格）+ 约束” → 输出按成本排序、带依据的材料组合方案。

两阶段：
- 无实验数据：窗口筛选 → 约束内生成候选 → 价格表算成本 → LLM 用材料卡/文献卡做定性五项评估 → “降本额 × 过关把握”排序。
- 有实验数据（已训练模型）：对候选逐个预测五项 → P(过关) → P ≥ 0.8 下按成本排序，探索型另列。
价格是唯一成本来源；输入里带的价格会先登记进价格表并触发联动。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import db, llm, pricing
from .config import KB, settings
from .formulation import constraints as C
from .formulation import cost as COST
from .formulation import export as X
from .formulation import library as L
from .formulation import materials as MAT
from .formulation import qualitative as Q
from .formulation import screening as S
from .formulation.generator import Candidate, evaluate, generate, ladder_candidates
from .pipeline import task

MATERIAL_INPUT_FIELDS = ("code", "name", "category", "grade", "producer", "supplier", "mfi", "density", "melting_point", "vicat",
                         "comonomer", "is_recycled", "role", "typical_min", "typical_max", "risk", "use_flag", "notes",
                         "tds_dart", "tds_seal_init", "proc_temp", "needs_ppa", "availability")


@dataclass
class RecommendInput:
    product: dict[str, Any] = field(default_factory=dict)
    base_formulation: dict[str, float] | None = None
    materials: list[dict[str, Any]] = field(default_factory=list)
    only_listed_materials: bool = False
    constraints: dict[str, Any] = field(default_factory=dict)
    n_schemes: int = 20
    n_generated: int = 40
    use_llm: bool = True
    use_ml: bool = True
    save_to_library: bool = True
    origin: str = "配方推荐页"   # 进配方库时的来源标签：对话推荐 / 配方推荐页
    seed: int = 42

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RecommendInput":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def example_input() -> dict[str, Any]:
    t = task.load()
    return {
        "product": {"name": t["product"]["name"], "structure": t["product"]["structure"],
                    "thickness_um": t["product"].get("thickness_um")},
        "base_formulation": {k: v["pct"] for k, v in t["current_formulation"].items()},
        "materials": [
            {"code": "LL", "name": "现用 LLDPE", "density": 0.918, "melting_point": 122, "mfi": 2.0, "comonomer": "丁烯", "price": 12000,
             "price_type": "actual", "price_source": "甲方口述 2026-09"},
            {"code": "LLC", "name": "国产 C4 LLDPE 7042", "density": 0.918, "melting_point": 122, "mfi": 2.0, "comonomer": "丁烯", "price": 8400,
             "price_type": "estimate", "price_source": "生意社 2026-09"},
            {"code": "R1", "name": "再生高压一级透明料", "density": 0.922, "mfi": 2.0, "is_recycled": 1, "price": 8000, "price_type": "actual",
             "price_source": "甲方口述 2026-09"},
        ],
        "only_listed_materials": False,
        "constraints": {},
        "n_schemes": 20,
        "use_llm": True,
        "use_ml": True,
    }


def _apply_materials(mats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把输入材料写入原料库与价格表；返回价格变更列表。"""
    changed = []
    old_prices = MAT.current_prices()
    for m in mats:
        code = str(m.get("code", "")).strip()
        if not code:
            continue
        data = {k: m[k] for k in MATERIAL_INPUT_FIELDS if k in m and m[k] not in (None, "")}
        data.setdefault("name", code)
        data["active"] = 1
        if "effects" in m and isinstance(m["effects"], dict):
            data["effects"] = m["effects"]
        MAT.upsert_material(data)
        price = m.get("price")
        if price not in (None, ""):
            ptype = m.get("price_type") or "estimate"
            old = old_prices.get(code, {}).get(ptype)
            if old != float(price):
                MAT.add_price(code, float(price), ptype, source=str(m.get("price_source") or "智能体输入"), supplier=str(m.get("supplier") or ""),
                              price_date=str(m.get("price_date") or ""))
                changed.append({"code": code, "old": old, "new": float(price), "type": ptype, "source": m.get("price_source", "智能体输入")})
    return changed


def _ml_scores(cands: list[Candidate]) -> dict[int, dict[str, Any]] | None:
    """有模型与 base 时返回 {index: {p_pass, pred}}；否则 None。"""
    from .ml import dataset as D
    from .ml.models import current_models, predict
    from .ml.optimize import _p_pass

    if not current_models():
        return None
    agg = D.aggregate(D.load())
    base = D.base_stats(agg) if not agg.empty else {}
    if not base:
        return None
    thr = D.thresholds(base)
    targets = [t for t in D.PRIMARY_TARGETS if t in thr]
    mats = {m["code"]: m for m in MAT.list_materials(active_only=False)}
    pred = predict([cd.components for cd in cands], targets=targets, mats=mats)
    if pred.empty:
        return None
    p = _p_pass(pred, thr, targets)
    out = {}
    for i in range(len(cands)):
        out[i] = {"p_pass": float(p[i]), "pred": {t: {"mean": float(pred.iloc[i][t + "_mean"]), "sd": float(pred.iloc[i].get(t + "_sd") or 0)}
                                                  for t in targets if t + "_mean" in pred}}
    return out


def recommend(inp: RecommendInput | dict[str, Any]) -> dict[str, Any]:
    inp = inp if isinstance(inp, RecommendInput) else RecommendInput.from_dict(inp)
    notes: list[str] = []
    before = pricing.snapshot()
    changed = _apply_materials(inp.materials)
    if changed:
        pricing.on_price_change("智能体输入价格", changed, before)
        notes.append(f"输入中有 {len(changed)} 项价格变更，已登记并重算配方库成本。")
    # 约束覆盖（本次运行内生效，不落盘）
    c = C.load()
    if inp.base_formulation:
        c["base_formulation"] = {k: float(v) for k, v in inp.base_formulation.items() if float(v) > 0}
        c.pop("cost_limit_auto", None)
        c["cost_limit"] = "auto"
        c["cost_target"] = "auto"
        c = C.resolve_cost_limits(c)
    for k, v in (inp.constraints or {}).items():
        if v not in (None, "", "auto"):
            c[k] = v
    base = C.base_formulation(c)
    base_cost = COST.compute_simple(base)
    structure = inp.product.get("structure", "mono")
    listed = [str(m["code"]) for m in inp.materials if m.get("code")]
    allowed = listed if (inp.only_listed_materials and listed) else None

    # 1) 替代品窗口筛选（基准 = base 里用量最大的新料）
    ref = max((k for k in base if not (MAT.get_material(k) or {}).get("is_recycled")), key=lambda k: base[k], default="LL")
    try:
        scr = S.screen(reference=ref)
        screening_rows = S.to_rows(scr)
    except ValueError as e:
        scr, screening_rows = None, []
        notes.append(str(e))

    # 2) 候选：生成 + 配方库
    cands = generate(n_out=inp.n_generated, structure=structure, seed=inp.seed,
                     cost_limit=c["cost_limit"], allowed=allowed)
    # 保守的阶梯方案（1:1 同类替代、再生料阶梯、茂金属低比例）——先保性能再看成本
    for cd in ladder_candidates(structure=structure):
        if allowed and any(k not in allowed and k != "AD" for k in cd.components):
            continue
        if cd.cost is not None and cd.cost < c["cost_limit"]:
            cands.append(cd)
    for f in L.list_formulations():
        if f["status"] not in ("候选", "推荐") or f.get("structure", "mono") != structure:
            continue
        if allowed and any(k not in allowed and k != "AD" for k in f["components"]):
            continue
        cd = evaluate(f["components"], structure=f["structure"])
        if cd.issues or cd.cost is None or cd.cost >= c["cost_limit"]:
            continue
        cd.theme = f["code"]
        cands.append(cd)
    seen: dict[tuple, Candidate] = {}
    for cd in cands:
        sig = cd.signature()
        if sig not in seen or (cd.cost or 1e9) < (seen[sig].cost or 1e9):
            seen[sig] = cd
    cands = sorted(seen.values(), key=lambda x: x.cost or 1e9)
    if not cands:
        notes.append("在当前约束与价格下没有比现配方更便宜的可行方案。")
    # 送 LLM 评估的数量上限：每个思路保留最便宜的几个，再按成本补齐（控制耗时与费用）
    n_assess = max(inp.n_schemes + 8, 24)
    if len(cands) > n_assess:
        per_theme: dict[str, int] = {}
        keep: list[Candidate] = []
        rest: list[Candidate] = []
        for cd in cands:
            quota = 1 if ("阶梯" in cd.theme or "1:1" in cd.theme or "低比例替代" in cd.theme or "50%" in cd.theme) else 3
            if per_theme.get(cd.theme, 0) < quota:
                per_theme[cd.theme] = per_theme.get(cd.theme, 0) + 1
                keep.append(cd)
            else:
                rest.append(cd)
        cands = sorted((keep + rest)[:n_assess], key=lambda x: x.cost or 1e9)
        notes.append(f"候选较多，只对成本最低且覆盖各思路的 {len(cands)} 个做定性评估。")

    # 3) 定性评估（LLM）
    if inp.use_llm and settings.llm_ready and cands:
        assessments = Q.assess(cands)
        mode = "知识驱动（LLM 定性评估）"
    else:
        assessments = [{"expected": "（未做 LLM 定性评估）", "pass_confidence": "中", "effects": {}, "risks": "", "risk_level": "", "evidence": ""} for _ in cands]
        mode = "仅成本与约束排序"
        if inp.use_llm and not settings.llm_ready:
            notes.append("未配置 LLM：四项性能定性评估跳过，方案仅按成本与约束排序。")

    # 4) 模型打分（有实验数据时）
    ml = _ml_scores(cands) if (inp.use_ml and cands) else None
    ranked = Q.rank(cands, assessments, top_n=max(inp.n_schemes, len(cands)))
    if ml:
        mode += " + 模型预测 P(过关)"
        by_formula = {cd.text(): i for i, cd in enumerate(cands)}
        for r in ranked:
            i = by_formula.get(r["formula"])
            r["ml"] = ml.get(i) if i is not None else None
        ranked.sort(key=lambda r: (-(1 if (r.get("ml") or {}).get("p_pass", 0) >= 0.8 else 0), r["cost"] or 1e9))
    ranked = ranked[:inp.n_schemes]
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    # 5) 首轮实验建议（LLM 可选）
    experiments: dict[str, Any] = {}
    if inp.use_llm and settings.llm_ready and ranked:
        from .pipeline.stage_report import pick_first_round

        top_for_pick = [{"rank": r["rank"], "formula": r["formula"], "cost": r["cost"], "savings_pct": round(r["savings_pct"] or 0, 1),
                         "expected": r.get("expected"), "risk": r.get("risks"), "confidence": r.get("pass_confidence")} for r in ranked]
        experiments = pick_first_round(top_for_pick, task.brief())

    # 6) 入库与导出
    out_at = db.now()
    codes = []
    if inp.save_to_library:
        for r in ranked:
            code = _existing_code(r["components"]) or L.next_code("A")
            L.save_formulation(code, r["components"], structure=structure,
                               predicted={"effects_short": r.get("effects_short"), "effects": r.get("effects"), "pass_confidence": r.get("pass_confidence"),
                                          "expected": r.get("expected"), "ml": r.get("ml")},
                               rationale=f"[{r['theme']}] {r.get('expected', '')}｜依据：{r.get('evidence', '')}",
                               risks=f"{r.get('risk_level', '')}：{r.get('risks', '')}", priority=str(r["rank"]), status="候选",
                               origin=f"{inp.origin} {out_at[:10]}")
            codes.append(code)
    from . import basedata

    for r in ranked:
        ml_ = r.get("ml") or {}
        if ml_.get("pred") and basedata.is_ready():
            r["judge"] = basedata.judge({k: v["mean"] for k, v in ml_["pred"].items()})
    price_basis = {m["code"]: {"price": m["price"], "tier": m["price_tier"]} for m in MAT.list_materials(active_only=True) if m.get("price") is not None}
    out = {
        "at": db.now(), "mode": mode, "product": {"structure": structure, **inp.product},
        "base_formulation": base, "base_cost": base_cost, "cost_limit": c["cost_limit"], "cost_target": c["cost_target"],
        "reference_material": ref, "screening": screening_rows, "windows": (scr or {}).get("windows"),
        "schemes": [{k: v for k, v in r.items() if k != "score"} for r in ranked], "library_codes": codes,
        "base": basedata.load(), "thresholds": basedata.thresholds() if basedata.is_ready() else {},
        "experiments": experiments, "price_basis": price_basis, "notes": notes,
    }
    run_id = db.insert("recommend_runs", {"at": out["at"], "input_json": db.dumps(asdict(inp)), "output_json": db.dumps(out), "note": mode})
    out["run_id"] = run_id
    out["outputs"] = [str(p) for p in export(out)]
    return out


def _existing_code(components: dict[str, float]) -> str | None:
    f = L.find_by_components(components)
    return f["code"] if f else None


def export(out: dict[str, Any], stem: str | None = None) -> list[Path]:
    stem = stem or f"智能体方案_{out['at'][:16].replace(':', '')}"
    xlsx = KB["formulation"] / f"{stem}.xlsx"
    rows = []
    for r in out["schemes"]:
        ml = r.get("ml") or {}
        rows.append({"排名": r["rank"], "设计思路": r["theme"], "配方（组分+比例%）": r["formula"], "成本(元/吨)": r["cost"], "价格口径": r["cost_tier"],
                     "较现配方降本(元/吨)": round(r["savings"]) if r.get("savings") is not None else None,
                     "降本%": round(r["savings_pct"], 1) if r.get("savings_pct") is not None else None, "面积成本指数": r.get("area_index"),
                     "四项预期(拉 撕 穿 封 透)": r.get("effects_short"), "预期说明": r.get("expected"), "风险": r.get("risks"), "风险等级": r.get("risk_level"),
                     "过关把握": r.get("pass_confidence"), "依据": r.get("evidence"),
                     "模型 P(过关)": round(ml["p_pass"], 2) if ml.get("p_pass") is not None else None,
                     "模型预测": "; ".join(f"{k} {v['mean']:.3g}±{v['sd']:.2g}" for k, v in (ml.get("pred") or {}).items())})
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="方案", index=False)
        pd.DataFrame(out["screening"]).to_excel(xw, sheet_name="替代品筛选", index=False)
        pd.DataFrame([{"代码": k, "价格": v["price"], "口径": v["tier"]} for k, v in out["price_basis"].items()]).to_excel(xw, sheet_name="价格口径", index=False)
        pd.DataFrame([{"项": "现配方", "值": COST.format_components(out["base_formulation"])}, {"项": "现配方成本", "值": out["base_cost"]},
                      {"项": "成本上限", "值": out["cost_limit"]}, {"项": "模式", "值": out["mode"]}, {"项": "生成时间", "值": out["at"]},
                      *[{"项": "说明", "值": n} for n in out["notes"]]]).to_excel(xw, sheet_name="口径", index=False)
        if out.get("experiments", {}).get("picks"):
            pd.DataFrame(out["experiments"]["picks"]).to_excel(xw, sheet_name="首轮试验建议", index=False)
    js = KB["formulation"] / f"{stem}.json"
    js.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    inq = X.inquiry_list(out["schemes"], path=KB["price"] / "询价清单.xlsx") if out["schemes"] else None
    paths = [xlsx, js] + ([inq] if inq else [])
    if out.get("schemes"):
        # 第 1 名方案的采购清单：每种料买谁家、什么价、怎么联系
        from . import procurement

        try:
            p = procurement.plan(out["schemes"][0].get("components") or {}, 1.0, name=f"第 1 名 · {out['schemes'][0].get('formula')}")
            paths.append(procurement.export(p, KB["price"] / f"采购清单_{stem}.xlsx"))
            out["procurement"] = {"total": p["total"], "cost_per_ton": p["cost_per_ton"], "missing": p["missing"],
                                  "items": [{k: v for k, v in it.items() if k != "alternatives"} for it in p["items"]]}
        except Exception:  # noqa: BLE001
            pass
    return paths


def last_runs(limit: int = 10) -> list[dict[str, Any]]:
    rows = db.q("SELECT id, at, note FROM recommend_runs ORDER BY id DESC LIMIT ?", (limit,))
    return rows


def load_run(run_id: int) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM recommend_runs WHERE id=?", (run_id,))
    return db.loads(r["output_json"], None) if r else None
