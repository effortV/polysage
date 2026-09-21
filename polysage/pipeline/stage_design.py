"""④ 候选配方与成本：约束内生成 + 配方库种子 → 成本 → LLM 定性五项评估 → 排序 → Top 20 + 询价清单 → 存配方库。"""
from __future__ import annotations

from typing import Any, Callable

from .. import db
from ..config import KB
from ..formulation import constraints as C
from ..formulation import export as X
from ..formulation import library as L
from ..formulation import qualitative as Q
from ..formulation.generator import Candidate, evaluate, generate
from . import state, task


def candidate_pool(structure: str, n_generated: int = 40, seed: int = 42) -> list[Candidate]:
    cands = generate(n_out=n_generated, structure=structure, seed=seed)
    # 配方库里已有的候选（流水线、对话、手工录入）一并评估
    for f in L.list_formulations():
        if f["status"] not in ("候选", "推荐"):
            continue
        if structure == "mono" and f["structure"] != "mono":
            continue
        cd = evaluate(f["components"], structure=f["structure"])
        if cd.issues or cd.cost is None or cd.cost >= C.load().get("cost_limit", 8000):
            continue
        cd.theme = f["code"]
        cands.append(cd)
    # 去重（同签名保留成本低者）
    seen: dict[tuple, Candidate] = {}
    for cd in cands:
        sig = cd.signature()
        if sig not in seen or (cd.cost or 1e9) < (seen[sig].cost or 1e9):
            seen[sig] = cd
    return list(seen.values())


def run(echo: Callable[[str], None] | None = None, *, n_generated: int = 40, top_n: int = 20, seed: int = 42) -> dict[str, Any]:
    t = task.load()
    structure = t["product"].get("structure", "mono")
    with state.running("design", echo):
        L.seed_top20()
        pool = candidate_pool(structure, n_generated=n_generated, seed=seed)
        state.log(f"候选池 {len(pool)} 个（生成 + 配方库），开始定性评估", echo)
        assessments = Q.assess(pool)
        ranked = Q.rank(pool, assessments, top_n=top_n)
        top_path = X.export_top20(ranked)
        inq_path = X.inquiry_list(ranked)
        # 存配方库：流水线编号 P01..；已有编号（Fxx）的更新评估
        saved = []
        for r in ranked:
            code = r["theme"] if r["theme"] in {f["code"] for f in L.list_formulations()} else None
            if not code:
                code = _find_or_next(r["components"])
            L.save_formulation(code, r["components"], structure=structure,
                               predicted={"effects_short": r.get("effects_short"), "effects": r.get("effects"),
                                          "pass_confidence": r.get("pass_confidence"), "expected": r.get("expected")},
                               rationale=f"[{r['theme']}] {r.get('expected', '')}｜依据：{r.get('evidence', '')}",
                               risks=f"{r.get('risk_level', '')}：{r.get('risks', '')}", priority=str(r.get("rank")),
                               status="候选", origin="流水线 ④")
            saved.append(code)
        summary = {"n_pool": len(pool), "n_top": len(ranked), "codes": saved, "outputs": [str(top_path), str(inq_path)],
                   "top": [{"rank": r["rank"], "formula": r["formula"], "cost": r["cost"], "savings_pct": round(r["savings_pct"] or 0, 1),
                            "expected": r.get("expected"), "risk": r.get("risks"), "confidence": r.get("pass_confidence")} for r in ranked]}
        state.set_stage("design", **summary)
    return summary


def _find_or_next(components: dict[str, float]) -> str:
    key = db.dumps(dict(sorted(components.items())))
    for f in L.list_formulations():
        if db.dumps(dict(sorted(f["components"].items()))) == key:
            return f["code"]
    return L.next_code("P")
