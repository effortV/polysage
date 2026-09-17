"""① 资料采集：主题 → LLM 生成检索式 → 多源检索 → LLM 判相关 → 入库（尽力全文）→ 生成卡片 → 来源清单。"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import pandas as pd

from .. import db, kb, llm
from ..config import KB
from ..ingest import ingest_hit
from ..ingest.cards import make_literature_card
from ..sources import search_all
from ..sources.base import SearchHit, dedupe
from . import state, task

# 主题清单（内部技术路线附录 B + 机理主题）。kind: paper | patent | web
TOPICS: list[dict[str, Any]] = [
    {"key": "blend_mechanism", "name": "共混体系与现配方机理", "kind": "paper",
     "zh": ["LLDPE LDPE 共混 吹膜 力学性能", "HDPE 共混 透明 撕裂 薄膜", "LLDPE LDPE HDPE 三元共混 包装膜 配方"],
     "en": ["LLDPE LDPE blend blown film mechanical properties", "HDPE LLDPE blend film haze tear strength",
            "LLDPE LDPE HDPE ternary blend blown film properties"]},
    {"key": "metallocene", "name": "茂金属 LLDPE", "kind": "paper",
     "zh": ["茂金属聚乙烯 落镖 穿刺 减薄", "mLLDPE 热封 起封温度"],
     "en": ["metallocene LLDPE blown film dart impact puncture downgauging", "mLLDPE heat seal initiation temperature film",
            "metallocene polyethylene LDPE blend bubble stability blown film"]},
    {"key": "recycled", "name": "再生聚乙烯", "kind": "paper",
     "zh": ["再生聚乙烯 薄膜 性能 凝胶", "PCR 聚乙烯 吹膜 配方 相容剂"],
     "en": ["recycled polyethylene film properties gel", "post-consumer recycled PE blown film formulation",
            "mechanical recycling LDPE film degradation antioxidant"]},
    {"key": "toughening", "name": "增韧与热封改性", "kind": "paper",
     "zh": ["POE 增韧 聚乙烯 薄膜", "EVA 共混 热封 透明 薄膜"],
     "en": ["POE polyolefin elastomer toughening polyethylene film", "EVA LLDPE blend heat seal clarity film",
            "plastomer LLDPE blend seal strength blown film"]},
    {"key": "filler", "name": "填充降本", "kind": "paper",
     "zh": ["碳酸钙 填充 聚乙烯 薄膜 力学 雾度"],
     "en": ["CaCO3 filled polyethylene film mechanical haze", "calcium carbonate masterbatch LLDPE blown film"]},
    {"key": "additives", "name": "加工助剂与稳定剂", "kind": "paper",
     "zh": ["含氟 加工助剂 鲨鱼皮 LLDPE", "抗氧剂 再生 聚乙烯 稳定"],
     "en": ["fluoropolymer processing aid melt fracture LLDPE", "antioxidant recycled polyethylene stabilization"]},
    {"key": "process", "name": "吹膜工艺与性能", "kind": "paper",
     "zh": ["吹胀比 霜线 撕裂 各向异性 LLDPE", "模口间隙 LLDPE 薄膜 性能"],
     "en": ["blow-up ratio frost line tear anisotropy LLDPE", "die gap LLDPE blown film properties"]},
    {"key": "patents", "name": "配方专利", "kind": "patent",
     "zh": ["聚乙烯 包装膜 再生料 配方", "三层 共挤 聚乙烯 膜 芯层 回收料", "茂金属 聚乙烯 包装膜 组合物"],
     "en": ["polyethylene packaging film composition recycled", "three-layer coextruded PE film core recycled layer",
            "metallocene linear low density polyethylene film composition LDPE blend"]},
    {"key": "tds_price", "name": "供应商 TDS 与价格", "kind": "web",
     "zh": ["茂金属 LLDPE 牌号 吹膜 技术参数 TDS", "POE 牌号 8150 8200 技术参数", "LLDPE 7042 价格 生意社",
            "LDPE 2426H 价格 卓创", "HDPE 5000S 价格", "再生 PE 颗粒 一级 透明 价格"],
     "en": ["ELITE 5400G technical data sheet blown film", "Exceed 1018 technical data sheet", "Engage 8150 technical data sheet"]},
]

SCREEN_PROMPT = (
    "任务：{brief}\n\n下面是检索到的资料题录。请逐条判断是否与“聚乙烯吹膜包装膜配方/性能/降本”相关，输出 JSON："
    "{{\"items\": [{{\"index\": 序号, \"relevant\": true/false, \"kind\": \"mechanism/material/process/patent/tds/price/other\", "
    "\"value\": \"一句话：这条资料对本项目有什么用（没有就写无）\"}}]}}\n\n题录：\n{listing}"
)

QUERY_PROMPT = (
    "任务：{brief}\n\n主题：{topic}。已有检索式：{seed}\n"
    "请补充 2 条更精准的英文检索式和 1 条中文检索式（面向学术数据库/专利库，用关键词而不是句子）。"
    "输出 JSON：{{\"queries\": [\"...\"]}}"
)


def plan_queries(topic: dict[str, Any], brief: str, use_llm: bool = True) -> list[str]:
    # 学术库（OpenAlex/Crossref/Scopus）对中文检索式噪声很大，文献主题只用英文；中文文献靠 CNKI/万方 手动上传补充。
    # 专利（Google Patents）与网页搜索中英文都用。
    seeds = list(topic.get("en", [])) if topic.get("kind") == "paper" else list(topic.get("zh", [])) + list(topic.get("en", []))
    if not use_llm:
        return seeds
    try:
        data = llm.chat_json([{"role": "system", "content": "你是文献检索员，输出严格 JSON。"},
                              {"role": "user", "content": QUERY_PROMPT.format(brief=brief, topic=topic["name"], seed="；".join(seeds))}],
                             max_tokens=600)
        extra = [q for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
        if topic.get("kind") == "paper":
            extra = [q for q in extra if not re.search(r"[一-鿿]", q)]
    except Exception:  # noqa: BLE001
        extra = []
    out = seeds + [q for q in extra if q not in seeds]
    return out[:8]


def screen(hits: list[SearchHit], brief: str, batch: int = 15) -> list[tuple[SearchHit, dict[str, Any]]]:
    """LLM 判相关；未配置 LLM 时全部保留（标 value=未筛）。"""
    kept: list[tuple[SearchHit, dict[str, Any]]] = []
    for i in range(0, len(hits), batch):
        part = hits[i:i + batch]
        listing = "\n".join(f"{j + 1}. [{h.source_type}] {h.title} ({h.year or ''}; {h.venue[:40]})\n   {(h.abstract or '')[:350]}"
                            for j, h in enumerate(part))
        try:
            data = llm.chat_json([{"role": "system", "content": "你是资料筛选员，输出严格 JSON。"},
                                  {"role": "user", "content": SCREEN_PROMPT.format(brief=brief, listing=listing)}], max_tokens=2500)
            items = {int(it.get("index")): it for it in data.get("items", []) if str(it.get("index", "")).isdigit()}
        except llm.LLMNotConfigured:
            kept.extend((h, {"relevant": True, "kind": h.source_type, "value": "未筛（LLM 未配置）"}) for h in part)
            continue
        except Exception as e:  # noqa: BLE001
            state.log(f"筛选批次失败，保留全部：{e}")
            kept.extend((h, {"relevant": True, "kind": h.source_type, "value": "未筛（LLM 出错）"}) for h in part)
            continue
        for j, h in enumerate(part):
            it = items.get(j + 1)
            if it and it.get("relevant"):
                kept.append((h, it))
    return kept


def run(echo: Callable[[str], None] | None = None, *, topics: list[str] | None = None, max_hits_per_query: int | None = None,
        max_keep: int = 160, make_cards: bool = True, use_llm_queries: bool = True) -> dict[str, Any]:
    t = task.load()
    brief = task.brief(t)
    year_from = t["search"].get("year_from")
    per_q = max_hits_per_query or t["search"].get("max_hits_per_query", 15)
    selected = [tp for tp in TOPICS if not topics or tp["key"] in topics]
    all_hits: list[SearchHit] = []
    errors: dict[str, str] = {}
    with state.running("collect", echo):
        for tp in selected:
            queries = plan_queries(tp, brief, use_llm=use_llm_queries)
            providers = {"paper": ["openalex", "crossref", "scopus"], "patent": ["google_patents"], "web": ["web"]}[tp["kind"]]
            for q in queries:
                hits, errs = search_all(q, providers=providers, limit=per_q, year_from=year_from if tp["kind"] == "paper" else None)
                for h in hits:
                    h.meta["topic"] = tp["key"]
                    h.meta["query"] = q
                all_hits.extend(hits)
                errors.update({f"{p}:{q[:30]}": e for p, e in errs.items()})
                state.log(f"[{tp['name']}] {q} → {len(hits)} 条", echo)
        merged = dedupe(all_hits)
        state.log(f"检索合计 {len(all_hits)} 条，去重后 {len(merged)} 条；开始相关性筛选", echo)
        kept = screen(merged, brief)
        kept = kept[:max_keep]
        state.log(f"保留 {len(kept)} 条，开始入库（尽力抓全文）", echo)
        rows = []
        ingested: list[int] = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(ingest_hit, h, True): (h, it) for h, it in kept}
            for f in as_completed(futs):
                h, it = futs[f]
                try:
                    sid, note = f.result()
                    ingested.append(sid)
                except Exception as e:  # noqa: BLE001
                    sid, note = None, f"入库失败：{str(e)[:80]}"
                rows.append({"source_id": sid, "题名": h.title, "类型": h.source_type, "来源库": h.provider, "年份": h.year,
                             "期刊/申请人": h.venue, "DOI": h.doi, "链接": h.url, "OA全文": bool(h.oa_pdf_url), "入库说明": note,
                             "主题": h.meta.get("topic"), "检索式": h.meta.get("query"), "资料价值": it.get("value", ""),
                             "可信度": h.credibility})
        n_cards = 0
        if make_cards:
            for sid in ingested:
                src = db.q1("SELECT source_type, n_chunks FROM sources WHERE id=?", (sid,))
                if not src or (src["n_chunks"] or 0) == 0:
                    continue
                if db.q1("SELECT id FROM cards WHERE source_id=?", (sid,)):
                    continue
                try:
                    make_literature_card(sid)
                    n_cards += 1
                except llm.LLMNotConfigured:
                    state.log("LLM 未配置，跳过卡片生成", echo)
                    break
                except Exception as e:  # noqa: BLE001
                    state.log(f"卡片生成失败 #{sid}：{str(e)[:80]}", echo)
        path = KB["project"] / "来源清单.xlsx"
        pd.DataFrame(rows).sort_values(["主题", "类型", "年份"], ascending=[True, True, False]).to_excel(path, index=False)
        summary = {"n_hits": len(all_hits), "n_unique": len(merged), "n_kept": len(kept), "n_ingested": len(ingested),
                   "n_cards": n_cards, "errors": errors, "outputs": [str(path)]}
        state.set_stage("collect", **summary)
    return summary


def suggest_manual_uploads() -> list[str]:
    """CNKI/万方无接口：列出建议手动补充的中文检索题名/关键词。"""
    out = []
    for tp in TOPICS:
        if tp["kind"] == "paper":
            out.extend(f"{tp['name']}：{q}" for q in tp.get("zh", []))
    return out
