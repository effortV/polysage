"""测试用的候选材料池：正式库只预置现配方在用的 4 种料，测试要验证筛选/推荐逻辑，需要一批候选。

这就是早期内部技术路线附录 D 的那份清单，价格是假设值——只在测试里用，不进正式库。
"""
from __future__ import annotations

from typing import Any

from polysage.formulation.materials import add_price, current_prices, upsert_material

CANDIDATES: list[dict[str, Any]] = [
    dict(code="LL", category="主体树脂", name="现用 LLDPE（牌号待确认）", grade="待甲方提供", mfi=2.0, density=0.918, comonomer="丁烯（待确认）", is_recycled=0,
         role="韧性骨架；现用主料，成本大头，替代对象", typical_min=0, typical_max=60, melting_point=122, vicat=100,
         effects={"拉伸": "基准", "撕裂": "基准", "穿刺": "基准", "热封": "基准"}, risk="现用；若为特殊牌号，替代实为降规格，需以其 TDS 为筛选基准",
         use_flag="是（基准）", price=12000, price_note="甲方口述价（2026-09）"),
    dict(code="LLC", category="主体树脂", name="国产 C4 LLDPE（7042 类）", grade="7042 / 218W / DFDA-7042", mfi=2.0, density=0.918, comonomer="丁烯", is_recycled=0,
         role="同类更便宜的 LLDPE：直接替代现用 LLDPE 的首选", typical_min=10, typical_max=60, melting_point=122, vicat=100,
         effects={"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "≈"}, risk="与现用牌号的 TDS 对比确认；副牌/宽规格料更便宜但批次波动大",
         use_flag="是（核心）", price=8400),
    dict(code="LL6", category="主体树脂", name="C6 LLDPE（齐格勒己烯共聚）", grade="按供应商", mfi=1.0, density=0.918, comonomer="己烯", is_recycled=0,
         role="韧性略优于 C4 的低价替代", typical_min=10, typical_max=30, melting_point=124,
         effects={"拉伸": "≈", "撕裂": "≈/↑", "穿刺": "≈/↑", "热封": "≈"}, risk="货源与牌号需确认", use_flag="是", price=8900),
    dict(code="mLL", category="主体树脂", name="茂金属 LLDPE（C6）", grade="1018/2045 类、国产茂金属", mfi=1.0, density=0.918, comonomer="己烯", is_recycled=0,
         role="高效韧性来源；替代 C4 时用量可减半以上", typical_min=10, typical_max=20, melting_point=118,
         effects={"拉伸": "≈", "撕裂": "≈/↑", "穿刺": "↑↑", "热封": "↑"}, risk="熔体强度低、挤出压力高，需 PPA；LDPE 类需 ≥35% 稳泡", use_flag="是（核心）", price=9400),
    dict(code="mLL8", category="主体树脂", name="茂金属 LLDPE（C8，Elite 类）", grade="Elite 5400 类", mfi=1.0, density=0.916, comonomer="辛烯", is_recycled=0,
         role="落镖/穿刺效率最高", typical_min=8, typical_max=15, melting_point=116,
         effects={"拉伸": "≈", "撕裂": "↑", "穿刺": "↑↑", "热封": "↑"}, risk="价格高、货源", use_flag="是", price=9900),
    dict(code="LD", category="主体树脂", name="LDPE 新料（2426H 类）", grade="2426H/2426K", mfi=2.0, density=0.922, comonomer="", is_recycled=0,
         role="加工性、膜泡稳定、光学、热封", typical_min=5, typical_max=20, melting_point=110,
         effects={"拉伸": "↓", "撕裂": "↓", "穿刺": "↓", "热封": "↑"}, risk="比 LLDPE 贵", use_flag="是（少量）", price=9600),
    dict(code="HD", category="主体树脂", name="HDPE 膜料（5000S 类）", grade="5000S/HHMTR144", mfi=0.9, density=0.950, comonomer="", is_recycled=0,
         role="挺度（撕裂与外观代价大，默认 ≤5%）", typical_min=0, typical_max=10, melting_point=130,
         effects={"拉伸": "↑", "撕裂": "↓↓", "穿刺": "↓", "热封": "↓"}, risk="撕裂下降、发白", use_flag="减量/去除", price=8400),
    dict(code="MD", category="主体树脂", name="MDPE 膜料", grade="按供应商", mfi=1.0, density=0.935, comonomer="", is_recycled=0,
         role="挺度与成本折中", typical_min=0, typical_max=15, melting_point=125,
         effects={"拉伸": "↑", "撕裂": "↓", "穿刺": "≈", "热封": "≈/↓"}, risk="雾度", use_flag="试探", price=8700),
    dict(code="R1", category="再生料", name="再生高压一级透明料", grade="—", mfi=2.0, density=0.922, comonomer="", is_recycled=1,
         role="现用；成本主力（比例上限靠阶梯实验定）", typical_min=15, typical_max=60, melting_point=112,
         effects={"拉伸": "略降", "撕裂": "略降", "穿刺": "↓（凝胶）", "热封": "略降（杂质）"}, risk="批次波动；需进厂快检",
         use_flag="是", price=8000, price_note="甲方口述价（2026-09）"),
    dict(code="R2", category="再生料", name="再生高压二级料", grade="—", mfi=2.0, density=0.925, comonomer="", is_recycled=1,
         role="更低成本（三层芯层）", typical_min=0, typical_max=40, melting_point=112,
         effects={"拉伸": "↓", "撕裂": "↓", "穿刺": "↓", "热封": "↓"}, risk="晶点、杂质", use_flag="三层芯层用", price=5300),
    dict(code="RL", category="再生料", name="再生线性一级料（工业膜回料）", grade="—", mfi=2.0, density=0.920, comonomer="", is_recycled=1,
         role="补韧性的低价料", typical_min=10, typical_max=20, melting_point=122,
         effects={"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "≈"}, risk="含 PIB 的缠绕膜回料会粘连", use_flag="是", price=5900),
    dict(code="PCR", category="再生料", name="混合 PE 消费后回料（造粒）", grade="—", mfi=1.5, density=0.930, comonomer="", is_recycled=1,
         role="最低成本（默认不用）", typical_min=10, typical_max=20, melting_point=120,
         effects={"拉伸": "↓", "撕裂": "↓", "穿刺": "↓", "热封": "↓"}, risk="需相容剂；气味；非透明", use_flag="默认不用（气味/颜色）", price=4800),
    dict(code="RHD", category="再生料", name="再生 HDPE", grade="—", mfi=0.8, density=0.950, comonomer="", is_recycled=1,
         role="挺度", typical_min=0, typical_max=10, melting_point=130,
         effects={"拉伸": "↑", "撕裂": "↓↓", "穿刺": "↓", "热封": "↓"}, risk="颜色、气味", use_flag="否（颜色/气味）", price=5600),
    dict(code="SC", category="再生料", name="甲方自有边角料回粒", grade="—", mfi=2.0, density=0.921, comonomer="", is_recycled=1,
         role="成本接近零", typical_min=5, typical_max=15, melting_point=118,
         effects={"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "≈"}, risk="需造粒设备", use_flag="是（若有）", price=800),
    dict(code="POE", category="韧性/热封改性", name="POE（乙烯-辛烯）", grade="Engage 8150/8200 类", mfi=1.0, density=0.875, comonomer="辛烯", is_recycled=0,
         role="高效增韧", typical_min=1, typical_max=4, melting_point=60,
         effects={"拉伸": "≈", "撕裂": "↑", "穿刺": "↑", "热封": "≈/↑"}, risk=">4% 影响挺度与透明", use_flag="是（少量）", price=14000),
    dict(code="POP", category="韧性/热封改性", name="POP 塑性体", grade="Affinity/Exact 类", mfi=1.0, density=0.902, comonomer="辛烯", is_recycled=0,
         role="增韧 + 热封", typical_min=2, typical_max=5, melting_point=90,
         effects={"拉伸": "≈", "撕裂": "≈/↑", "穿刺": "↑", "热封": "↑"}, risk="价格", use_flag="备选", price=13000),
    dict(code="VL", category="韧性/热封改性", name="VLDPE / ULDPE（密度 <0.91）", grade="按供应商", mfi=1.0, density=0.905, comonomer="辛烯/己烯", is_recycled=0,
         role="极高韧性与热封", typical_min=5, typical_max=10, melting_point=105,
         effects={"拉伸": "↓", "撕裂": "↑", "穿刺": "↑↑", "热封": "↑↑"}, risk="价格高、挺度降", use_flag="备选", price=10500),
    dict(code="EVA", category="韧性/热封改性", name="EVA（VA 5～9%，膜级）", grade="按供应商", mfi=2.0, density=0.930, comonomer="VA", is_recycled=0,
         role="热封、柔韧", typical_min=3, typical_max=8, melting_point=95,
         effects={"拉伸": "↓", "撕裂": "≈", "穿刺": "≈", "热封": "↑↑"}, risk="粘连、气味（高 VA）", use_flag="是（少量）", price=10500),
    dict(code="EMA", category="韧性/热封改性", name="EMA / EBA", grade="按供应商", mfi=2.0, density=0.940, comonomer="MA/BA", is_recycled=0,
         role="类似 EVA，耐热更好", typical_min=3, typical_max=8, melting_point=90,
         effects={"拉伸": "↓", "撕裂": "≈", "穿刺": "≈", "热封": "↑"}, risk="价格", use_flag="备选", price=12000),
    dict(code="OBC", category="韧性/热封改性", name="OBC 烯烃嵌段共聚物", grade="Infuse 类", mfi=1.0, density=0.877, comonomer="辛烯", is_recycled=0,
         role="增韧、耐热", typical_min=2, typical_max=5, melting_point=120,
         effects={"拉伸": "≈", "撕裂": "↑", "穿刺": "↑", "热封": "≈"}, risk="价格高", use_flag="否（成本）", price=16000),
    dict(code="ION", category="韧性/热封改性", name="离聚物 / EAA", grade="Surlyn / Primacor 类", mfi=2.0, density=0.940, comonomer="AA", is_recycled=0,
         role="热封与热粘性", typical_min=2, typical_max=5, melting_point=95,
         effects={"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "↑↑"}, risk="价格高", use_flag="否（成本）", price=18000),
    dict(code="FL", category="填充", name="CaCO₃ 填充母料（80%）", grade="按供应商", mfi=None, density=1.950, comonomer="", is_recycled=0,
         role="降本（默认不用）", typical_min=8, typical_max=20,
         effects={"拉伸": "↓", "撕裂": "↓", "穿刺": "↓", "热封": "↓"}, risk="密度上升；按面积计价不划算", use_flag="默认不用（改变外观）", price=3800),
    dict(code="TFL", category="填充", name="细粒径填充母料（CaCO₃/BaSO₄）", grade="按供应商", mfi=None, density=1.600, comonomer="", is_recycled=0,
         role="降本，外观影响较小", typical_min=5, typical_max=10,
         effects={"拉伸": "≈/↓", "撕裂": "≈/↓", "穿刺": "≈/↓", "热封": "≈"}, risk="改变外观", use_flag="试探", price=5200),
    dict(code="TALC", category="填充", name="滑石粉母料", grade="按供应商", mfi=None, density=1.700, comonomer="", is_recycled=0,
         role="挺度、阻隔", typical_min=3, typical_max=8,
         effects={"拉伸": "≈", "撕裂": "↓", "穿刺": "↓", "热封": "↓"}, risk="改变外观", use_flag="否", price=4500),
    dict(code="AD", category="功能助剂", name="功能助剂母料（PPA + 抗氧）", grade="按供应商", mfi=None, density=0.950, comonomer="", is_recycled=0,
         role="消鲨鱼皮、允许高再生比例；稳定再生料", typical_min=0.5, typical_max=1.5,
         effects={"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "≈"}, risk="与滑石类抗粘连剂相互作用", use_flag="是（必配）", price=12500),
    dict(code="CP", category="功能助剂", name="相容剂（MAH-g-PE）", grade="按供应商", mfi=None, density=0.920, comonomer="", is_recycled=0,
         role="再生料含杂质时用", typical_min=1, typical_max=2, melting_point=125,
         effects={"拉伸": "≈/↑", "撕裂": "≈/↑", "穿刺": "≈/↑", "热封": "≈"}, risk="价格；优先靠采购规格避免杂质", use_flag="条件使用", price=18000),
    dict(code="NUC", category="功能助剂", name="成核剂（PE 用）", grade="按供应商", mfi=None, density=1.000, comonomer="", is_recycled=0,
         role="略提挺度", typical_min=0.05, typical_max=0.2,
         effects={"拉伸": "≈", "撕裂": "≈", "穿刺": "≈", "热封": "≈"}, risk="效果在 PE 里有限", use_flag="试探", price=80000),
    dict(code="ADR", category="功能助剂", name="扩链剂（ADR 类）", grade="按供应商", mfi=None, density=1.100, comonomer="", is_recycled=0,
         role="修复降解再生料的分子量", typical_min=0.1, typical_max=0.3,
         effects={"拉伸": "≈/↑", "撕裂": "≈", "穿刺": "≈/↑", "热封": "≈"}, risk="过量凝胶", use_flag="试探", price=60000),
]


def seed_candidates() -> int:
    """把候选材料池写进临时库，连同早期那套假设价——测试要的是筛选/推荐逻辑，不是价格真伪。"""
    n = 0
    for m in CANDIDATES:
        m = dict(m)
        m.pop("price_note", None)
        price = m.pop("price")
        effects = m.pop("effects")
        upsert_material({**m, "effects": effects, "origin": "测试候选池"})
        if price and current_prices().get(m["code"], {}).get("estimate") is None:
            add_price(m["code"], price, "estimate", source="测试候选池假设价", price_date="2026-09")
        n += 1
    return n
