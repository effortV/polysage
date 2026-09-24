"""③ 替代材料发现：按功能寻找候选料 → 定向检索 TDS/文献 → 材料卡 → 网查参考价（带来源）→ 候选材料全清单。"""
from __future__ import annotations

import re
from typing import Any, Callable

import pandas as pd

from .. import db, kb, llm
from ..config import KB
from ..formulation.materials import add_price, current_prices, list_materials, seed_materials, upsert_material
from ..ingest import ingest_hit
from ..ingest.cards import make_material_card
from ..sources import search_all
from ..sources.fetch import fetch_url
from ..sources.websearch import search as web_search
from . import state, task

# 定向检索式：材料代码 → (英文文献检索式, 中文 TDS/网页检索式, 价格检索式)
QUERIES: dict[str, tuple[str, str, str]] = {
    "LL": ("butene LLDPE blown film properties 7042", "LLDPE 7042 吹膜 技术参数", "LLDPE 7042 价格 生意社"),
    "LL6": ("hexene LLDPE Ziegler-Natta blown film tear dart", "己烯共聚 LLDPE 吹膜 牌号 参数", "己烯 LLDPE 价格"),
    "mLL": ("metallocene LLDPE hexene blown film dart puncture seal", "茂金属 LLDPE 吹膜 牌号 技术参数", "茂金属聚乙烯 价格 mLLDPE"),
    "mLL8": ("octene metallocene LLDPE Elite blown film properties", "ELITE 5400G 技术参数 吹膜", "Elite 5400 价格"),
    "LD": ("LDPE blown film bubble stability optics blend LLDPE", "LDPE 2426H 技术参数 吹膜", "LDPE 2426H 价格"),
    "HD": ("HDPE film grade blend LLDPE stiffness haze", "HDPE 5000S 技术参数", "HDPE 5000S 价格"),
    "MD": ("MDPE film grade blend LLDPE stiffness", "MDPE 薄膜料 牌号 参数", "MDPE 价格"),
    "R1": ("recycled LDPE film pellets quality gel content", "再生 PE 颗粒 一级 透明 吹膜", "再生 PE 颗粒 一级 透明 价格"),
    "R2": ("recycled LDPE secondary grade film core layer", "再生 PE 二级料 吹膜 芯层", "再生 PE 二级料 价格"),
    "RL": ("recycled LLDPE stretch film regrind pellets properties", "再生 LLDPE 线性 回料 颗粒 缠绕膜 PIB", "再生 LLDPE 颗粒 价格"),
    "PCR": ("post-consumer recycled polyethylene film blend compatibilizer odor", "PCR 聚乙烯 回料 造粒 薄膜", "PCR PE 回料 价格"),
    "POE": ("polyolefin elastomer POE LLDPE blend film toughness", "POE 弹性体 8150 8200 技术参数 增韧", "POE 价格 8150"),
    "POP": ("polyolefin plastomer LLDPE blend seal strength film", "POP 塑性体 Affinity Exact 参数", "POP 塑性体 价格"),
    "VL": ("VLDPE ULDPE blend film puncture seal", "VLDPE 超低密度聚乙烯 薄膜 牌号", "VLDPE 价格"),
    "EVA": ("EVA LLDPE blend film heat seal clarity vinyl acetate 5-9", "EVA 薄膜级 VA 含量 吹膜 参数", "EVA 薄膜级 价格"),
    "EMA": ("ethylene methyl acrylate copolymer film blend seal", "EMA 乙烯-丙烯酸甲酯 薄膜 牌号", "EMA 价格"),
    "FL": ("calcium carbonate filled polyethylene film mechanical haze", "碳酸钙 填充母粒 薄膜 参数", "碳酸钙 填充母粒 价格"),
    "TFL": ("transparent filler masterbatch barium sulfate polyethylene film haze", "透明填充母粒 硫酸钡 薄膜", "透明填充母粒 价格"),
    "AD": ("fluoropolymer processing aid LLDPE melt fracture antioxidant masterbatch", "PPA 加工助剂母粒 吹膜 参数", "PPA 加工助剂母粒 价格"),
    "CP": ("maleic anhydride grafted polyethylene compatibilizer recycled film", "马来酸酐接枝 PE 相容剂 参数", "相容剂 PE-g-MAH 价格"),
    "SC": ("in-house film regrind reuse blown film quality", "边角料 回收 造粒 吹膜", ""),
}

PRICE_PROMPT = (
    "从下面网页正文中提取材料【{name}】的最新市场价格。输出 JSON：{{\"found\": true/false, \"price_rmb_per_ton\": 数字或null, "
    "\"date\": \"YYYY-MM-DD 或 YYYY-MM\", \"basis\": \"报价口径（如：华东市场 出厂价/含税）\", \"quote\": \"原文中的价格句子\"}}。"
    "只提取明确写出的价格；单位不是元/吨时换算；没有就 found=false。\n\n网页：{url}\n正文：\n{text}"
)

NEW_MATERIAL_PROMPT = (
    "任务：{brief}\n\n现配方机理报告的“替代空间”一节如下：\n{space}\n\n"
    "现有候选材料代码：{codes}\n"
    "请补充最多 5 种不在现有清单中的候选材料（须是聚乙烯吹膜包装膜中实际可用、国内可采购的），输出 JSON："
    "{{\"materials\": [{{\"code\": \"大写字母代码\", \"name\": \"中文名（英文缩写）\", \"category\": \"主体树脂/再生料/韧性热封改性/填充/功能助剂\", "
    "\"role\": \"替代什么功能\", \"typical_min\": 数, \"typical_max\": 数, \"why\": \"理由\"}}]}}；没有就返回空列表。"
)


def _extract_price(name: str, hit_url: str, text: str) -> dict[str, Any] | None:
    try:
        data = llm.chat_json([{"role": "system", "content": "你是价格信息抽取员，输出严格 JSON。"},
                              {"role": "user", "content": PRICE_PROMPT.format(name=name, url=hit_url, text=text[:6000])}], max_tokens=400)
    except Exception:  # noqa: BLE001
        return None
    if data.get("found") and data.get("price_rmb_per_ton"):
        try:
            p = float(str(data["price_rmb_per_ton"]).replace(",", ""))
        except ValueError:
            return None
        if 300 <= p <= 200000:
            return {"price": p, "date": str(data.get("date") or ""), "basis": data.get("basis", ""), "quote": data.get("quote", "")}
    return None


def estimate_price(code: str, name: str, query: str, echo: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """网查参考价：搜索 → 抓 2 个页面 → LLM 抽取 → 登记 estimate（带 URL 与日期）。"""
    if not query:
        return None
    try:
        hits = web_search(query, limit=6)
    except Exception as e:  # noqa: BLE001
        state.log(f"价格搜索失败 {code}：{e}", echo)
        return None
    for h in hits[:3]:
        try:
            page = fetch_url(h.url)
        except Exception:  # noqa: BLE001
            continue
        if not page["text"]:
            continue
        got = _extract_price(name, h.url, page["text"])
        if got:
            old_est = current_prices().get(code, {}).get("estimate")
            deviates = old_est is not None and abs(got["price"] / old_est - 1) > 0.4
            # 与现有估计价偏差 > 40% 时不自动采用，记为待核对（price_type=estimate_candidate，不参与成本计算）
            ptype = "estimate_candidate" if deviates else "estimate"
            add_price(code, got["price"], ptype, source=f"网查参考价：{h.title[:60]}｜{got['basis']}｜{got['quote'][:80]}",
                      url=h.url, price_date=got["date"], note="流水线 ③ 自动抽取，需人工核对" + ("；与现有估计价偏差>40%，未采用" if deviates else ""))
            state.log(f"参考价 {code} = {got['price']:.0f} 元/吨（{got['date']}，{h.url[:50]}）{'【偏差大，待核对】' if deviates else ''}", echo)
            return {**got, "url": h.url, "adopted": not deviates}
    return None


def discover_new_materials(brief: str, echo: Callable[[str], None] | None = None) -> int:
    """（已停用）早期让模型凭印象列候选料的做法：没有来源网址，材料真假不可查。
    现在改走 polysage.scout.discover()——先搜网页、再从网页里抽料，每条带来源。保留本函数只为旧脚本兼容。"""
    report = kb.read_text(KB["project"] / "现配方机理报告.md")
    m = re.search(r"## 4[^\n]*\n(.*?)(?=\n## |\Z)", report, flags=re.S)
    space = m.group(1).strip() if m else "（机理报告尚未生成）"
    codes = [x["code"] for x in list_materials(active_only=False)]
    try:
        data = llm.chat_json([{"role": "system", "content": "你是材料库管理员，输出严格 JSON。"},
                              {"role": "user", "content": NEW_MATERIAL_PROMPT.format(brief=brief, space=space[:3000], codes=", ".join(codes))}],
                             max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        state.log(f"新材料建议失败：{e}", echo)
        return 0
    n = 0
    for it in data.get("materials", []) or []:
        code = re.sub(r"[^A-Za-z0-9]", "", str(it.get("code", "")))[:8]
        if not code or code in codes or not it.get("name"):
            continue
        upsert_material({"code": code, "name": it["name"], "category": it.get("category", ""), "role": it.get("role", ""),
                         "typical_min": it.get("typical_min"), "typical_max": it.get("typical_max"), "use_flag": "待评估",
                         "notes": f"流水线 ③ 建议：{it.get('why', '')}"})
        n += 1
        state.log(f"新增候选材料 {code}：{it['name']}", echo)
    return n


def run(echo: Callable[[str], None] | None = None, *, codes: list[str] | None = None, do_prices: bool = True,
        do_cards: bool = True, discover: bool = True) -> dict[str, Any]:
    t = task.load()
    brief = task.brief(t)
    with state.running("scout", echo):
        seed_materials()                      # 只种现配方在用的 4 种料
        if discover:
            from .. import scout as _scout

            out = _scout.discover("", depth="标准", max_new=8, echo=lambda msg: state.log(msg, echo))
            state.log(f"智能体发现新料 {len(out['added'])} 种（每种都带来源网址）", echo)
            # 不再让模型凭空列候选料：没有来源网址的材料不进原料库
        mats = [m for m in list_materials(active_only=False) if not codes or m["code"] in codes]
        mats = [m for m in mats if not str(m.get("use_flag", "")).startswith("否")]
        n_cards = n_prices = 0
        for m in mats:
            code, name = m["code"], m["name"]
            q_en, q_zh, q_price = QUERIES.get(code, (f"{name} polyethylene film blend properties", f"{name} 技术参数 吹膜", f"{name} 价格"))
            # 定向文献 + 网页 TDS 入库
            try:
                hits, _ = search_all(q_en, providers=["openalex", "crossref"], limit=5)
                whits, _ = search_all(q_zh, providers=["web"], limit=4)
                for h in hits[:4] + whits[:3]:
                    h.meta["topic"] = f"material:{code}"
                    ingest_hit(h, fetch_fulltext=True)
            except Exception as e:  # noqa: BLE001
                state.log(f"定向检索失败 {code}：{str(e)[:80]}", echo)
            if do_cards:
                try:
                    cid, cits = make_material_card(name)
                    n_cards += 1
                    card = kb.get_card(cid)
                    d = card["data"] if card else {}
                    # 把卡片中的关键参数回写原料库（只写有出处的）
                    upd = {"code": code, "name": name}
                    for k_card, k_mat in (("mfr", "mfi"), ("density", "density"), ("comonomer", "comonomer"), ("risk", "risk"), ("role", "role")):
                        v = d.get(k_card)
                        if v not in (None, "", "待查", "资料未提供") and k_mat in ("risk", "role", "comonomer"):
                            upd[k_mat] = str(v)[:200]
                    if isinstance(d.get("effects"), dict):
                        upd["effects"] = d["effects"]
                    upsert_material(upd)
                    state.log(f"材料卡 {code}：{name}（引用 {len(cits)} 条）", echo)
                except llm.LLMNotConfigured:
                    state.log("LLM 未配置，跳过材料卡", echo)
                    do_cards = False
                except Exception as e:  # noqa: BLE001
                    state.log(f"材料卡失败 {code}：{str(e)[:80]}", echo)
            if do_prices:
                try:
                    if estimate_price(code, name, q_price, echo):
                        n_prices += 1
                except llm.LLMNotConfigured:
                    do_prices = False
        path = export_candidates()
        summary = {"n_materials": len(mats), "n_cards": n_cards, "n_prices": n_prices, "outputs": [str(path)]}
        state.set_stage("scout", **summary)
    return summary


def export_candidates(path=None):
    path = path or KB["project"] / "候选材料全清单.xlsx"
    rows = []
    prices = current_prices()
    cards = {c["name"]: c for c in kb.list_cards("material")}
    for m in list_materials(active_only=False):
        eff = m.get("effects") or {}
        card = cards.get(m["name"])
        ev = (card or {}).get("data", {}).get("evidence", "")
        pr = db.q1("SELECT source, url, price_date FROM prices WHERE material_code=? AND price_type='estimate' ORDER BY id DESC", (m["code"],)) or {}
        rows.append({"代码": m["code"], "类别": m.get("category"), "材料": m["name"], "牌号示例": m.get("grade"), "MFR": m.get("mfi"),
                     "密度": m.get("density"), "共聚单体": m.get("comonomer"), "作用": m.get("role"),
                     "典型用量%": f"{m.get('typical_min')}～{m.get('typical_max')}",
                     "拉伸": eff.get("拉伸"), "撕裂": eff.get("撕裂"), "穿刺": eff.get("穿刺"), "热封": eff.get("热封"), "透明": eff.get("透明"),
                     "估计价(元/吨)": prices.get(m["code"], {}).get("estimate"), "价格来源": pr.get("source"), "价格链接": pr.get("url"),
                     "价格日期": pr.get("price_date"), "实价(元/吨)": prices.get(m["code"], {}).get("actual"),
                     "风险": m.get("risk"), "是否用于配方": m.get("use_flag"), "依据（材料卡）": str(ev)[:300], "备注": m.get("notes")})
    pd.DataFrame(rows).to_excel(path, index=False)
    return path
