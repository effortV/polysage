"""供 DeepSeek 函数调用的工具：schema + 执行器。每个工具返回给模型的是简洁文本（带 [S#] 引用时同时返回 citations）。"""
from __future__ import annotations

import json
from typing import Any, Callable

from .. import db, kb
from ..rag.answer import build_context, cite_label
from ..rag.index import search as kb_search_raw

ToolFn = Callable[..., tuple[str, list[dict[str, Any]]]]

_REGISTRY: dict[str, tuple[dict[str, Any], ToolFn]] = {}


def tool(name: str, description: str, params: dict[str, Any], required: list[str] | None = None):
    def deco(fn: ToolFn):
        schema = {"type": "function", "function": {"name": name, "description": description,
                                                    "parameters": {"type": "object", "properties": params,
                                                                   "required": required or []}}}
        _REGISTRY[name] = (schema, fn)
        return fn
    return deco


def schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [_REGISTRY[n][0] for n in (names or list(_REGISTRY)) if n in _REGISTRY]


def run(name: str, args: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if name not in _REGISTRY:
        return f"未知工具 {name}", []
    try:
        return _REGISTRY[name][1](**(args or {}))
    except TypeError as e:
        return f"参数错误：{e}", []
    except Exception as e:  # noqa: BLE001
        return f"工具执行失败：{type(e).__name__}: {str(e)[:400]}", []


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------- 知识库 ----------------

@tool("kb_search", "在本地知识库（已入库的文献、专利、TDS、网页、上传资料）中做混合检索，返回带 [S#] 编号的片段与出处。",
      {"query": {"type": "string", "description": "检索问题或关键词，中英文均可"},
       "top_k": {"type": "integer", "description": "返回片段数，默认 8"},
       "source_types": {"type": "array", "items": {"type": "string"}, "description": "限定类型：paper/patent/tds/web/price"}},
      ["query"])
def kb_search(query: str, top_k: int = 8, source_types: list[str] | None = None):
    hits = kb_search_raw(query, top_k=top_k, source_types=source_types)
    if not hits:
        return "知识库中没有相关片段。可用 literature_search / web_search / patent_search 联网检索并入库。", []
    ctx, cits = build_context(hits, max_chars=7000)
    legend = "\n".join(f"{c['label']}: {c['cite']}" for c in cits)
    return f"{ctx}\n\n出处：\n{legend}", cits


@tool("list_sources", "列出知识库里最近入库的资料（题名、类型、来源、年份、是否有全文）。",
      {"limit": {"type": "integer"}, "source_type": {"type": "string"}})
def list_sources(limit: int = 30, source_type: str | None = None):
    rows = db.list_sources(limit=limit, source_type=source_type)
    lines = [f"#{r['id']} [{r['source_type']}/{r['provider']}] {r['title'][:80]} ({r.get('year') or ''}) 块数={r.get('n_chunks')}" for r in rows]
    return "\n".join(lines) or "知识库为空", []


@tool("save_card", "把结论写成知识库卡片（材料卡 / 文献卡 / 专利卡）并保存为 Markdown。",
      {"card_type": {"type": "string", "enum": ["material", "literature", "patent"]},
       "name": {"type": "string", "description": "卡片名称（材料名或文献题名）"},
       "data_json": {"type": "string", "description": "字段字典的 JSON 字符串。材料卡：name/grade_examples/mfr/density/comonomer/role/effects{拉伸,撕裂,穿刺,热封,透明}/typical_dosage/price_estimate/risk/evidence；文献卡：citation/system/conclusions/data_points/credibility/relevance；专利卡：citation/applicant/formulation_range/claimed_effect/borrow/avoid/credibility"},
       "source_id": {"type": "integer", "description": "关联的资料 id（可选）"},
       "credibility": {"type": "integer", "description": "1-6"}},
      ["card_type", "name", "data_json"])
def save_card(card_type: str, name: str, data_json: str | dict, source_id: int | None = None, credibility: int = 3):
    data = data_json if isinstance(data_json, dict) else json.loads(data_json or "{}")
    cid = kb.save_card(card_type, name, data, source_id=source_id, credibility=credibility)
    return f"已保存 {card_type} 卡片 #{cid}：{name}", []


@tool("make_card", "让系统读取某条已入库资料的全文并自动生成文献卡/专利卡。", {"source_id": {"type": "integer"}}, ["source_id"])
def make_card(source_id: int):
    from ..ingest.cards import make_literature_card

    cid = make_literature_card(source_id)
    c = kb.get_card(cid)
    return f"已生成卡片 #{cid}：{c['name']}\n{_j(c['data'])[:1500]}", []


# ---------------- 联网检索 ----------------

@tool("literature_search", "联网检索学术文献（OpenAlex / Crossref / Scopus / Semantic Scholar），返回题录与摘要；不自动入库。",
      {"query": {"type": "string", "description": "英文关键词效果最好"}, "limit": {"type": "integer"},
       "year_from": {"type": "integer"}}, ["query"])
def literature_search(query: str, limit: int = 10, year_from: int | None = None):
    from ..sources import search_all

    hits, errors = search_all(query, providers=["openalex", "crossref", "scopus"], limit=limit, year_from=year_from)
    lines = []
    for h in hits[:limit]:
        lines.append(f"- [{h.provider}:{h.external_id}] {h.title} | {h.authors[:40]} | {h.venue} {h.year or ''} | DOI:{h.doi or '-'}"
                     f"{' | OA全文' if h.oa_pdf_url else ''}\n  摘要：{(h.abstract or '')[:300]}")
    _STASH.update({f"{h.provider}:{h.external_id}": h for h in hits})
    err = ("\n检索错误：" + _j(errors)) if errors else ""
    return ("\n".join(lines) or "无结果") + err + "\n（需要入库时调用 ingest_source，参数 ref 取方括号内的 provider:id）", []


@tool("patent_search", "联网检索专利（Google Patents），返回公开号、题名、申请人、摘要片段。",
      {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])
def patent_search(query: str, limit: int = 10):
    from ..sources.patents import search_google_patents

    hits = search_google_patents(query, limit=limit)
    _STASH.update({f"{h.provider}:{h.external_id}": h for h in hits})
    lines = [f"- [{h.provider}:{h.external_id}] {h.title} | {h.venue} | {h.year or ''} | {h.url}\n  {h.abstract[:250]}" for h in hits]
    return ("\n".join(lines) or "无结果") + "\n（入库调用 ingest_source，会抓取权利要求与说明书）", []


@tool("web_search", "通用网页搜索（价格行情、供应商 TDS、行业资讯）。结果可信度按域名分级；价格只能作估计价。",
      {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])
def web_search(query: str, limit: int = 8):
    from ..sources.websearch import search

    hits = search(query, limit=limit)
    _STASH.update({f"{h.provider}:{h.external_id}": h for h in hits})
    lines = [f"- [{h.provider}:{h.external_id}] {h.title} | 类型:{h.source_type} 可信度:{h.credibility}\n  {h.abstract[:200]}" for h in hits]
    return "\n".join(lines) or "无结果", []


@tool("fetch_url", "抓取指定网页或 PDF 的正文（不入库，仅阅读）。", {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"])
def fetch_url(url: str, max_chars: int = 6000):
    from ..sources.fetch import fetch_url as _f

    r = _f(url)
    return f"标题：{r['title']}\n类型：{r['kind']}\n正文（截断）：\n{r['text'][:max_chars]}", []


_STASH: dict[str, Any] = {}


@tool("ingest_source", "把 literature_search / patent_search / web_search 返回的某条结果入库（尽力抓取全文），或直接按 URL 入库。",
      {"ref": {"type": "string", "description": "provider:id，或 http 开头的 URL"},
       "source_type": {"type": "string", "description": "URL 入库时的类型：web/tds/price/paper/patent"},
       "credibility": {"type": "integer"}}, ["ref"])
def ingest_source(ref: str, source_type: str = "web", credibility: int = 5):
    from ..ingest import ingest_hit, ingest_url

    if ref.startswith("http"):
        sid, note = ingest_url(ref, source_type=source_type, credibility=credibility)
        return f"已入库 #{sid}：{note}", []
    hit = _STASH.get(ref)
    if not hit:
        return f"未找到 {ref}，请先检索", []
    sid, note = ingest_hit(hit, fetch_fulltext=True)
    return f"已入库 #{sid}（{hit.title[:60]}）：{note}", []


# ---------------- 原料与价格 ----------------

@tool("list_materials", "列出原料库（代码、名称、MFR、密度、作用、估计价/实价、典型用量、是否用于配方）。", {})
def list_materials():
    from ..formulation.materials import list_materials as _lm

    rows = _lm()
    lines = [f"{m['code']}: {m['name']} | MFR {m.get('mfi') or '-'} 密度 {m.get('density') or '-'} | {m.get('role', '')} | "
             f"用量 {m.get('typical_min')}-{m.get('typical_max')}% | 价 {m.get('price')}（{m['price_tier']}） | {m.get('use_flag', '')}"
             for m in rows]
    return "\n".join(lines), []


@tool("get_material", "查看某个材料的完整信息（含四项影响、风险、价格历史）。", {"code": {"type": "string"}}, ["code"])
def get_material(code: str):
    from ..formulation.materials import get_material as _gm, price_history

    m = _gm(code)
    if not m:
        return f"无材料 {code}", []
    hist = price_history(code)[:5]
    return _j({k: v for k, v in m.items() if k != "effects_json"}) + "\n价格历史：" + _j(hist), []


@tool("upsert_material", "新增或更新原料库条目。", {"code": {"type": "string"}, "name": {"type": "string"}, "category": {"type": "string"},
      "grade": {"type": "string"}, "supplier": {"type": "string"}, "mfi": {"type": "number"}, "density": {"type": "number"},
      "comonomer": {"type": "string"}, "is_recycled": {"type": "integer"}, "role": {"type": "string"},
      "typical_min": {"type": "number"}, "typical_max": {"type": "number"}, "effects": {"type": "object", "additionalProperties": {"type": "string"}, "description": "键：拉伸/撕裂/穿刺/热封/透明，值：方向+说明"},
      "risk": {"type": "string"}, "use_flag": {"type": "string"}, "notes": {"type": "string"}}, ["code", "name"])
def upsert_material(**kw):
    from ..formulation.materials import upsert_material as _um

    mid = _um({k: v for k, v in kw.items() if v is not None})
    return f"已保存材料 {kw['code']}（id={mid}）", []


@tool("screen_materials", "替代品窗口筛选：以基准材料（默认现用 LLDPE）的密度/熔点/MFR 生成窗口，按“物性相近且更便宜”列出候选并分组（同类替代/低比例改性/挺度替代/再生填充/热封改性）。",
      {"reference": {"type": "string", "description": "基准材料代码，默认 LL"}, "price_max": {"type": "number", "description": "价格上限，默认取基准价"}})
def screen_materials(reference: str = "LL", price_max: float | None = None):
    from ..formulation import screening as S

    res = S.screen(reference=reference, price_max=price_max)
    lines = [f"基准 {res['reference']['code']} {res['reference']['name']}：价 {res['reference']['price']}，密度 {res['reference']['density']}，熔点 {res['reference']['melting_point']}，MFR {res['reference']['mfi']}",
             f"窗口：{res['windows']}"]
    for g, lst in res["groups"].items():
        lines.append(f"## {g}")
        for r in lst:
            lines.append(f"- {r.code} {r.name} | 价 {r.price}（{r.price_tier}，较基准 {round(r.price_delta) if r.price_delta is not None else '?'}） | "
                         f"密度{r.fits['密度']} 熔点{r.fits['熔点']} MFR{r.fits['MFR']} 匹配 {r.fit_score} | 用量 {r.typical} | {r.verdict} | 风险：{r.risk}")
    return "\n".join(lines), []


@tool("recommend_schemes", "运行推荐智能体：按当前原料库与价格（可附带新材料/新价格）在约束内生成材料组合方案，按成本与过关把握排序，导出方案表与询价清单。",
      {"materials": {"type": "array", "items": {"type": "object", "properties": {"code": {"type": "string", "description": "材料代码，如 LLC、mLL、R1；新材料用大写字母代码"}, "name": {"type": "string"}, "category": {"type": "string"}, "grade": {"type": "string"}, "producer": {"type": "string"}, "supplier": {"type": "string"}, "density": {"type": "number"}, "melting_point": {"type": "number"}, "mfi": {"type": "number"}, "comonomer": {"type": "string"}, "is_recycled": {"type": "integer"}, "typical_min": {"type": "number"}, "typical_max": {"type": "number"}, "role": {"type": "string"}, "risk": {"type": "string"}, "price": {"type": "number", "description": "元/吨"}, "price_type": {"type": "string", "enum": ["estimate", "actual"]}, "price_source": {"type": "string", "description": "来源与日期，如：供应商A 报价 2026-09-16 / 生意社 2026-09"}}, "required": ["code"]}, "description": "可选：本次新增/更新的材料（含价格）；只用清单里的材料出方案时配合 only_listed_materials=true"},
       "only_listed_materials": {"type": "boolean"}, "n_schemes": {"type": "integer"}, "use_llm": {"type": "boolean"}})
def recommend_schemes(materials: list[dict] | None = None, only_listed_materials: bool = False, n_schemes: int = 8, use_llm: bool = True):
    from .. import recommender

    out = recommender.recommend({"materials": materials or [], "only_listed_materials": only_listed_materials, "n_schemes": n_schemes,
                                 "use_llm": use_llm, "save_to_library": True, "origin": "对话推荐"})
    lines = [f"模式：{out['mode']}；现配方成本 {out['base_cost']:.0f}，上限 {out['cost_limit']}"] + [f"· {n}" for n in out["notes"]]
    for s in out["schemes"]:
        ml = s.get("ml") or {}
        lines.append(f"{s['rank']}. [{s['theme']}｜{s.get('bucket') or '-'}] {s['formula']} | 成本 {s['cost']}（{s['cost_tier']}）| 降本 {s['savings_pct']:.1f}% | "
                     f"四项 {s.get('effects_short') or '-'}（{(s.get('parity') or {}).get('why', '')}）| 把握 {s.get('pass_confidence') or '-'}"
                     + (f" | P(过关) {ml['p_pass']:.2f}" if ml.get('p_pass') is not None else ""))
        if s.get("sourcing"):
            lines.append(f"    原料采购：{s['sourcing']}")
    lines.append("产出：" + "; ".join(out["outputs"]))
    if out.get("library_codes"):
        lines.append("已存入配方库（来源：对话推荐）：" + ", ".join(out["library_codes"]))
    return "\n".join(lines), []


@tool("register_material", "登记或更新一种材料（物性 + 价格）：写入原料库，价格进价格表并触发联动（成本重算、重排、变动报告）。"
      "用户报出材料信息时先调用它。原料库已有同类代码时（如国产 7042 → LLC，茂金属 → mLL，再生一级料 → R1）不要新建代码：material.code 直接用已有代码，"
      "或填 similar_to=已有代码，价格与牌号会登记到该代码下。全新材料才新建大写代码，系统会按物性自动归入约束分组。",
      {"similar_to": {"type": "string", "description": "可选：原料库中的同类已有代码（如 LLC）"}, "material": {"type": "object", "properties": {"code": {"type": "string", "description": "材料代码，如 LLC、mLL、R1；新材料用大写字母代码"}, "name": {"type": "string"}, "category": {"type": "string"}, "grade": {"type": "string"}, "producer": {"type": "string"}, "supplier": {"type": "string"}, "density": {"type": "number"}, "melting_point": {"type": "number"}, "mfi": {"type": "number"}, "comonomer": {"type": "string"}, "is_recycled": {"type": "integer"}, "typical_min": {"type": "number"}, "typical_max": {"type": "number"}, "role": {"type": "string"}, "risk": {"type": "string"}, "price": {"type": "number", "description": "元/吨"}, "price_type": {"type": "string", "enum": ["estimate", "actual"]}, "price_source": {"type": "string", "description": "来源与日期，如：供应商A 报价 2026-09-16 / 生意社 2026-09"}}, "required": ["code"]}}, ["material"])
def register_material(material: dict, similar_to: str | None = None):
    from .. import pricing
    from ..formulation import constraints as C
    from ..formulation.materials import get_material, upsert_material as _um

    m = dict(material or {})
    code = str(m.get("code", "")).strip()
    if not code:
        return "缺少材料代码", []
    price = m.pop("price", None)
    ptype = m.pop("price_type", None) or "estimate"
    psrc = m.pop("price_source", None) or "对话登记"
    existing = get_material(code)
    note = ""
    if not existing and similar_to and get_material(similar_to):
        # 已有同类代码：把本次牌号/供应商/物性写到该代码下，价格也登记在它名下
        base_m = get_material(similar_to)
        code = similar_to
        m["notes"] = f"{base_m.get('notes') or ''}；对话登记牌号 {material.get('grade') or material.get('name')}（{psrc}）".strip("；")
        m.pop("name", None)
        note = f"（按同类已有代码 {similar_to} 登记）"
        existing = base_m
    data = {k: v for k, v in m.items() if v not in (None, "")}
    data["code"] = code
    data.setdefault("name", (existing or {}).get("name") or code)
    data["active"] = 1
    _um(data)
    if not existing:
        group = C.register_code(code, data)
        note = f"（新代码，已自动归入约束分组 {group}）"
    msg = f"已登记材料 {code}（{data.get('name')}）{note}"
    if price not in (None, ""):
        res = pricing.record_price(code, float(price), ptype, source=psrc, supplier=str(m.get("supplier") or ""))
        msg += (f"；价格 {float(price):.0f} 元/吨（{ptype}，{psrc}）已联动：现配方成本 {res['base_cost']:.0f}，"
                f"{res['n_flagged']} 个配方成本/排名明显变化")
    return msg, []


@tool("set_base", "登记现用膜实测 base：4 个数——拉伸强度 tensile、撕裂强度 tear、穿刺力 puncture、热封强度 seal（用户若分别给 MD/TD，取平均后登记并说明），自动派生过关线。数值必须来自用户或实测报告。",
      {"tensile": {"type": "number", "description": "拉伸强度 MPa"}, "tear": {"type": "number", "description": "撕裂强度 N"},
       "puncture": {"type": "number", "description": "穿刺力 N"}, "seal": {"type": "number", "description": "热封强度 N/15mm"},
       "sd": {"type": "object", "additionalProperties": {"type": "number"}, "description": "可选：各项标准差，如 {\"tensile\": 1.2}"},
       "n": {"type": "object", "additionalProperties": {"type": "integer"}, "description": "可选：各项测试条数"},
       "thickness_um": {"type": "number"}, "seal_temp": {"type": "number"}, "note": {"type": "string"}})
def set_base(sd: dict | None = None, n: dict | None = None, thickness_um: float | None = None, seal_temp: float | None = None,
             note: str = "", **values):
    from .. import basedata

    items = {}
    for k, v in values.items():
        if k in basedata.LABEL and v not in (None, ""):
            items[k] = {"mean": float(v), "sd": (sd or {}).get(k), "n": (n or {}).get(k)}
    if not items:
        return "没有可登记的数值", []
    d = basedata.set_items(items, thickness_um=thickness_um, seal_temp=seal_temp, note=note)
    return "已登记 base：" + basedata.brief() + (f"\n仍缺：{'、'.join(basedata.missing(d))}" if basedata.missing(d) else "\n六项齐全，可判过关。"), []


@tool("get_base", "查看当前 base（现用膜实测）与过关线。", {})
def get_base():
    from .. import basedata

    return basedata.to_markdown(), []


@tool("undo_price", "撤销某材料最近一条价格记录（登记错了用），并联动重算。", {"code": {"type": "string"}, "price_type": {"type": "string", "enum": ["estimate", "actual"]}}, ["code"])
def undo_price(code: str, price_type: str | None = None):
    from .. import pricing

    res = pricing.undo_last_price(code, price_type)
    if not res:
        return f"{code} 没有可撤销的价格记录", []
    return f"已撤销 {code} 的最近一条 {res['deleted']['price_type']} 价 {res['deleted']['price']}；现配方成本 {res['base_cost']:.0f}，{res['n_flagged']} 个配方明显变化", []


@tool("scheme_trial_kit", "为某个推荐方案生成试验包：称料单（按批次公斤数）+ 预填配方的数据表模板，保存到 06_实验数据。",
      {"code": {"type": "string", "description": "配方库编号，如 A01 / F04"}, "batch_kg": {"type": "number"}, "n_samples": {"type": "integer"}}, ["code"])
def scheme_trial_kit(code: str, batch_kg: float = 25.0, n_samples: int = 3):
    from ..ml.dataset import make_trial_kit

    paths = make_trial_kit(code, batch_kg=batch_kg, n_samples=n_samples)
    return "已生成：" + "；".join(str(p) for p in paths), []


@tool("supplier_quotes", "查看某种材料已找到的厂商报价（网查/询价），与现价比较；没有结果时提示先寻源。",
      {"code": {"type": "string", "description": "材料代码，如 LL、LLC、R1"}, "top": {"type": "integer", "description": "返回条数，默认 8"}}, ["code"])
def supplier_quotes(code: str, top: int = 8):
    from .. import sourcing

    rows = [r for r in sourcing.compare(code) if r["kind"] != "行情"][:top]
    if not rows:
        return sourcing.summary_text(code) + " 可调用 find_suppliers 立即寻源。", []
    lines = [sourcing.summary_text(code)]
    for r in rows:
        d = f"{r['diff']:+.0f}（{r['diff_pct']:+.1f}%）" if r.get("diff") is not None else "-"
        lines.append(f"- {r['supplier']}（{r['kind']}，{r['region'] or '地区不详'}）{r['grade'] or ''} {r['price']:.0f} 元/吨，{r['basis'] or '口径不详'}，"
                     f"{r['quote_date'] or '日期不详'}，与现价 {d}，可信度 {r['credibility']}，来源 {r['source_url'][:60]}")
    return "\n".join(lines) + "\n（网查价需询价核实；在“供应商寻源”页可加入询价清单并登记实价）", []


@tool("find_suppliers", "按贸易商日报同样的口径（厂家+牌号+仓库地+状态+含税价）网查某类树脂近两周的报价作对照（约 1～2 分钟）；不进价格卡。",
      {"code": {"type": "string", "description": "材料代码，如 LL、LLC、R1"},
       "extra": {"type": "string", "description": "补充关键词，如地区、牌号"}}, ["code"])
def find_suppliers(code: str, extra: str = ""):
    from .. import daily_quotes
    from ..formulation.materials import get_material

    m = get_material(code)
    if not m:
        return f"没有材料 {code}", []
    fam = next((f for f, codes in daily_quotes.FAMILY_CODES.items() if code in codes), None)
    if not fam:
        return f"{code} 不属于 LLDPE/LDPE/HDPE/mLLDPE，网查只支持这四类。", []
    s = daily_quotes.web_market_quotes([fam], depth="快", pages_per_query=2)[fam]
    rows = daily_quotes.web_rows(fam)
    lines = [f"网查 {fam}：{s['pages']} 个页面，{s['rows']} 条近两周报价（新增 {s['new']}）。网查价只作对照，不进价格卡。"]
    for r in rows:
        lines.append(f"- {r['grade']} @ {r['warehouse'] or '未注明'}（{r['delivery'] or '-'}）含税 {r['price']:.0f} → 到厂 {r['landed_price']:.0f}，{r['trader']}，{r['quote_date']}")
    return "\n".join(lines) + "\n" + daily_quotes.picks_text(), []


@tool("import_trader_quotes", "把贸易商发来的当日报价原文（微信文字）解析入库，并按“每类树脂当日最低到厂价”更新价格卡实价。",
      {"text": {"type": "string", "description": "报价原文，可多行"}, "trader": {"type": "string", "description": "报价商名称"},
       "date": {"type": "string", "description": "报价日期 YYYY-MM-DD，默认今天"}}, ["text", "trader"])
def import_trader_quotes(text: str, trader: str, date: str = ""):
    from .. import daily_quotes

    res = daily_quotes.import_text(text, trader, date or None, apply=True)
    head = f"解析 {len(res['rows'])} 条，新增 {res['saved']['new']}、重复 {res['saved']['dup']}、跳过 {res['saved']['skip']}。"
    if res["applied"]:
        head += " 价格卡已更新：" + "；".join(f"{a['code']} → {a['landed']:.0f}（{a['producer']} {a['grade']}）" for a in res["applied"])
    return head + "\n" + daily_quotes.picks_text(res["date"]), []


@tool("price_outlook", "查看最新的价格预判（每类树脂 1 周 / 1 月预期、方向、依据）以及各候选方案在预期价格下的成本变化；refresh=true 时先重新计算（约 2 分钟）。",
      {"refresh": {"type": "boolean", "description": "是否重新计算预判"}})
def price_outlook(refresh: bool = False):
    from .. import forecast

    if refresh:
        forecast.run()
    text = forecast.outlook_text()
    rows = forecast.scheme_impact(top_n=8)
    if rows:
        text += "\n\n预期价格（1 月）下的方案成本：\n" + "\n".join(
            f"- {r['name']}：现 {r['cost_now']} → 预期 {r['cost_future']}（{r['delta']:+d}，{r['delta_pct']:+.1f}%）" for r in rows)
    return text, []


@tool("procurement_plan", "给出某个方案/配方的采购方案：每种料（含再生料、助剂、母料）买谁家、到厂价多少、联系方式、用量与小计，并导出采购清单 xlsx。",
      {"rank": {"type": "integer", "description": "最近一次推荐里的方案排名，默认 1"},
       "formulation_code": {"type": "string", "description": "或配方库编号，如 A03、G01"},
       "components": {"type": "object", "additionalProperties": {"type": "number"}, "description": "或直接给组分，如 {\"LL\": 35, \"R1\": 44, \"RL\": 20, \"AD\": 1}"},
       "tons": {"type": "number", "description": "批量（吨成品），默认 1"}})
def procurement_plan(rank: int = 1, formulation_code: str = "", components: dict | None = None, tons: float = 1.0):
    from .. import procurement
    from ..config import KB

    if components:
        p = procurement.plan({k: float(v) for k, v in components.items()}, tons)
    elif formulation_code:
        p = procurement.plan_for_code(formulation_code, tons)
    else:
        p = procurement.plan_for_scheme(rank, tons)
    if not p:
        return "没有找到对应的方案/配方（先 recommend_schemes 或给 components）。", []
    path = procurement.export(p, KB["price"] / "采购清单.xlsx")
    return procurement.text(p) + f"\n采购清单已导出：{path}", []


@tool("find_material_suppliers", "为再生料 / 助剂 / 母料等辅料按材料代码网查厂商与报价（R1、RL、R2、AD、POE、EVA、FL 等），入库后返回最低几家；可选把最低价写成估计价。",
      {"codes": {"type": "array", "items": {"type": "string"}, "description": "材料代码，如 [\"R1\", \"RL\", \"AD\"]"},
       "depth": {"type": "string", "enum": ["快", "标准", "深"]},
       "apply_estimate": {"type": "boolean", "description": "是否把最低到厂价写成估计价（默认 true）"},
       "force": {"type": "boolean", "description": "24 小时内查过且没查到的料默认跳过；只有用户明确要求重查时才传 true"}}, ["codes"])
def find_material_suppliers(codes: list[str], depth: str = "快", apply_estimate: bool = True, force: bool = False):
    from .. import daily_quotes

    summary = daily_quotes.web_material_quotes(codes, depth=depth, pages_per_query=2, force=force)
    applied = daily_quotes.apply_web_estimates(codes) if apply_estimate else []
    lines, misses = [], []
    for code, st in summary.items():
        if st.get("best"):
            b = st["best"]
            lines.append(f"{code} {st['name']}：{st['rows']} 条报价（新增 {st['new']}），"
                         f"最低到厂 {b['landed']:.0f}（{b['supplier'][:16]}，{b['region'] or '地区不详'}"
                         + (f"，电话 {b['contact']}" if b.get("contact") else "") + "）")
        else:
            misses.append(code)
            hint = daily_quotes.HARD_TO_SOURCE.get(code)
            cur = st.get("current")
            lines.append(f"{code} {st['name']}：公开渠道查不到报价"
                         + (f"（{hint}这类料本来就不在网上挂价，只能直接问厂家）" if hint else "")
                         + (f"（{st['skipped'][:16]} 刚查过，这次直接跳过）" if st.get("skipped") else "")
                         + (f"。成本先按现有估计价 {cur:.0f} 元/吨计入" if cur
                            else "。价格卡上也没有价，请按你的经验给一个数并在回答里标明是假设"))
        for r in daily_quotes.material_rows(code, top=3)[1:]:
            lines.append(f"    备选：{r['supplier'][:16]} {r['landed_price']:.0f}（{r['region'] or '地区不详'}"
                         + (f"，{r['contact']}" if r.get("contact") else "") + "）")
    if applied:
        lines.append("已更新估计价：" + "；".join(f"{a['code']} → {a['landed']:.0f}" for a in applied) + "（网查价，未询价核实）")
    if misses:
        lines.append(f"【{'、'.join(misses)} 查不到，到此为止】不要再换关键词、也不要用别的工具找这几个料。"
                     "直接在回答里写明“查不到公开报价，需向厂家询价”，按上面的价把成本算完，"
                     "把方案表、采购清单和导出给出来。")
    return "\n".join(lines) or "没有结果。", []


@tool("daily_picks", "查看最近一次贸易商日报里每类树脂（LLDPE/LDPE/HDPE）的最低到厂价与次选。", {})
def daily_picks():
    from .. import daily_quotes

    return daily_quotes.picks_text(), []


@tool("price_report", "读取最新的价格变动报告与最近价格事件（谁变了、现配方成本、哪些方案排名变化）。", {})
def price_report():
    from .. import pricing

    ev = pricing.events(5)
    head = "\n".join(f"- {e['at']} {e['trigger']}：base 成本 {round(e['summary'].get('base_cost') or 0)}，明显变化 {e['summary'].get('n_flagged')}" for e in ev) or "（尚无价格事件）"
    body = pricing.REPORT_PATH.read_text(encoding="utf-8")[:3000] if pricing.REPORT_PATH.exists() else ""
    return f"最近事件：\n{head}\n\n{body}", []


@tool("explain_scheme", "查看最近一次智能体推荐中某个方案的完整细节（四项方向与理由、依据、风险、成本口径、模型预测）。", {"rank": {"type": "integer", "description": "方案排名"}}, ["rank"])
def explain_scheme(rank: int):
    from .. import recommender

    runs = recommender.last_runs(1)
    if not runs:
        return "尚无推荐运行，请先调用 recommend_schemes", []
    out = recommender.load_run(runs[0]["id"]) or {}
    s = next((x for x in out.get("schemes", []) if x.get("rank") == rank), None)
    if not s:
        return f"没有排名 {rank} 的方案", []
    return _j({k: s.get(k) for k in ("rank", "theme", "formula", "cost", "cost_tier", "savings", "savings_pct", "density", "area_index",
                                       "effects", "expected", "risks", "risk_level", "pass_confidence", "evidence", "ml")}), []


@tool("add_price", "为材料登记价格并触发联动（全部配方成本重算、重排、写价格变动报告）：estimate=网查估计价（必须给来源与日期），actual=甲方实价。",
      {"code": {"type": "string"}, "price": {"type": "number"}, "price_type": {"type": "string", "enum": ["estimate", "actual"]},
       "source": {"type": "string"}, "url": {"type": "string"}, "price_date": {"type": "string"}, "supplier": {"type": "string"},
       "note": {"type": "string"}}, ["code", "price", "price_type", "source"])
def add_price(code: str, price: float, price_type: str, source: str, url: str = "", price_date: str = "", supplier: str = "", note: str = ""):
    from .. import pricing

    res = pricing.record_price(code, price, price_type, source=source, url=url, price_date=price_date, supplier=supplier, note=note)
    return (f"已登记 {code} {price_type} 价 {price} 元/吨；现配方成本 {res['base_cost']:.0f}；{res['n_flagged']} 个配方成本/排名明显变化；"
            f"最便宜前 3：{[(t['code'], round(t['cost'])) for t in res['top5'][:3]]}；报告 {pricing.REPORT_PATH.name}"), []


# ---------------- 配方 ----------------

@tool("compute_cost", "按价格卡核算配方成本（元/吨）、共混密度与面积成本指数，并检查约束。组分为重量 %，合计 100。",
      {"components": {"type": "object", "additionalProperties": {"type": "number"}, "description": "组分重量百分比，如 {\"LLC\": 45, \"LD\": 10, \"HD\": 5, \"R1\": 39, \"AD\": 1}，合计 100"},
       "structure": {"type": "string", "enum": ["mono", "ABA"]}}, ["components"])
def compute_cost(components: dict, structure: str = "mono"):
    from ..formulation.generator import evaluate

    cd = evaluate({k: float(v) for k, v in components.items()}, structure=structure)
    out = {"配方": cd.text(), "成本(元/吨)": cd.cost, "价格口径": cd.cost_tier, "较base降本": cd.savings,
           "降本%": round(cd.savings_pct, 1) if cd.savings_pct is not None else None, "密度": cd.density,
           "面积成本指数": cd.area_index, "约束问题": cd.issues or "无"}
    return _j(out), []


@tool("check_constraints", "只检查配方是否满足表 4-2 约束。", {"components": {"type": "object", "additionalProperties": {"type": "number"}, "description": "组分重量百分比，如 {\"LLC\": 45, \"LD\": 10, \"HD\": 5, \"R1\": 39, \"AD\": 1}，合计 100"}, "structure": {"type": "string"},
      "layer": {"type": "string", "enum": ["whole", "core", "skin"]}}, ["components"])
def check_constraints(components: dict, structure: str = "mono", layer: str = "whole"):
    from ..formulation import constraints as C

    issues = C.check({k: float(v) for k, v in components.items()}, structure=structure, layer=layer)
    return ("可行" if not issues else "违反：\n- " + "\n- ".join(issues)), []


@tool("generate_candidates", "在约束内按设计思路生成候选配方并核算成本（不含性能判断）。",
      {"n": {"type": "integer", "description": "数量，默认 20"},
       "themes": {"type": "array", "items": {"type": "string"}, "description": "限定思路：国产同类 LLDPE 替代/同类替代 + 再生料上调/茂金属置换/C8 茂金属/齐格勒 C6/再生线性料/EVA 保热封/POE 补韧/去 HDPE/MDPE 替代/二级料试探/温和版"},
       "fixed": {"type": "object", "additionalProperties": {"type": "number"}, "description": "强制固定的组分比例，如 {\"AD\": 1}"}})
def generate_candidates(n: int = 20, themes: list[str] | None = None, fixed: dict | None = None):
    from ..formulation.generator import generate

    cands = generate(n_out=n, themes=themes, fixed={k: float(v) for k, v in (fixed or {}).items()} or None)
    lines = [f"{i + 1}. [{c.theme}] {c.text()} | 成本 {c.cost}（{c.cost_tier}）| 降本 {c.savings_pct:.1f}% | 密度 {c.density} | 面积指数 {c.area_index}"
             for i, c in enumerate(cands)]
    return "\n".join(lines) or "没有满足约束且成本达标的候选", []


@tool("save_formulation", "把配方存入配方库（05_配方库）。", {"code": {"type": "string", "description": "编号，留空自动生成 Gxx"},
      "components": {"type": "object", "additionalProperties": {"type": "number"}, "description": "组分重量百分比，如 {\"LLC\": 45, \"LD\": 10, \"HD\": 5, \"R1\": 39, \"AD\": 1}，合计 100"}, "structure": {"type": "string"},
      "effects_short": {"type": "string", "description": "四项预期，如 ≈ ↑ ↑ ↑（拉 撕 穿 封）"}, "rationale": {"type": "string"},
      "risks": {"type": "string"}, "priority": {"type": "string"}, "status": {"type": "string"}}, ["components"])
def save_formulation(components: dict, code: str = "", structure: str = "mono", effects_short: str = "",
                     rationale: str = "", risks: str = "", priority: str = "", status: str = "候选"):
    from .. import db as _db
    from ..formulation.library import find_by_components, next_code, save_formulation as _sf

    comps = {k: float(v) for k, v in components.items() if float(v)}
    total = sum(comps.values())
    if abs(total - 100) > 0.5:
        return f"组分合计 {total:.1f}%，必须为 100%，请修正后再存。", []
    dup = find_by_components(comps)
    if dup and not code:
        return f"配方库里已有相同组分的配方 {dup['code']}（来源：{dup.get('origin') or '-'}），不重复入库。", []
    from ..formulation.library import NotPurchasable

    code = code or next_code("G")
    try:
        fid = _sf(code, comps, structure=structure, predicted={"effects_short": effects_short}, rationale=rationale, risks=risks,
                  priority=priority, status=status, origin=f"对话手工 {_db.now()[:10]}")
    except NotPurchasable as e:
        return (f"这个配方没有入库：{e}。配方库只收买得到的料——先用 find_material_suppliers 网查该料的厂商报价，"
                "或用 add_price 登记一个正式报价，再存一次。"), []
    return f"已保存配方 {code}（id={fid}，来源：对话手工）", []


@tool("list_formulations", "列出配方库中的配方（编号、配方、成本、状态、四项预期）。", {"status": {"type": "string"}})
def list_formulations(status: str | None = None):
    from ..formulation.library import list_formulations as _lf

    rows = _lf(status)
    lines = [f"{r['code']} [{r['structure']}] {r['formula']} | 估 {round(r['cost_estimate']) if r['cost_estimate'] else '-'} "
             f"实 {round(r['cost_actual']) if r['cost_actual'] else '-'} | {r['status']} | {(r['predicted'] or {}).get('effects_short', '')} | {r['priority']}"
             for r in rows]
    return "\n".join(lines) or "配方库为空", []


# ---------------- 数据与模型 ----------------

@tool("data_qc", "读取实验数据表（06_实验数据/data.csv）做质检并汇总样本数、base 统计与过关阈值。", {})
def data_qc():
    from ..ml import dataset as D

    df = D.load()
    if df.empty:
        return "数据表为空。模板在 06_实验数据/数据表模板.csv。", []
    issues = D.qc(df)
    agg = D.aggregate(df)
    base = D.base_stats(agg)
    thr = D.thresholds(base) if base else {}
    return _j({"行数": len(df), "样品数": int(agg["sample_id"].nunique()), "质检问题": issues or "无",
               "base": {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in base.items()},
               "过关阈值": {k: round(v, 3) for k, v in thr.items()}}), []


@tool("train_models", "训练四项性能模型并做 LOOCV 验证，返回各模型 RMSE 与验收结果。", {"targets": {"type": "array", "items": {"type": "string"}}})
def train_models(targets: list[str] | None = None):
    from ..formulation.materials import list_materials as _lm
    from ..ml import models as M

    mats = {m["code"]: m for m in _lm(active_only=False)}
    reports, summary = M.train_all(targets=targets, mats=mats)
    out = [{"target": r.target, "n": r.n, "best": r.best, "metrics": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in r.metrics.items()},
            "accepted": r.accepted, "note": r.note} for r in reports]
    return _j({"models": out, "pass_accuracy": summary.get("pass_accuracy"), "thresholds": summary.get("thresholds")}), []


@tool("model_status", "查看当前各性能模型（类型、样本数、LOOCV 指标、训练时间）。", {})
def model_status():
    from ..ml.models import current_models

    rows = current_models()
    return _j([{k: r[k] for k in ("target", "model_type", "n_samples", "metrics", "trained_at")} for r in rows]) or "尚无模型", []


@tool("predict_performance", "用当前模型预测若干配方的四项性能（均值 ± SD）。", {"formulations": {"type": "array", "items": {"type": "object", "additionalProperties": {"type": "number"}},
      "description": "配方列表，每个为组分字典"}}, ["formulations"])
def predict_performance(formulations: list[dict]):
    from ..formulation.materials import list_materials as _lm
    from ..ml.models import predict

    mats = {m["code"]: m for m in _lm(active_only=False)}
    df = predict([{k: float(v) for k, v in f.items()} for f in formulations], mats=mats)
    if df.empty:
        return "尚无已训练模型", []
    return df.round(3).to_string(), []


@tool("recommend_next", "AI+ML 联合推荐下一轮实验配方（利用型 + 探索型），含 P(过关)、成本、训练域距离。",
      {"n_exploit": {"type": "integer"}, "n_explore": {"type": "integer"}, "p_min": {"type": "number"}})
def recommend_next(n_exploit: int = 4, n_explore: int = 3, p_min: float = 0.8):
    from ..formulation.materials import list_materials as _lm
    from ..ml import dataset as D
    from ..ml.optimize import recommend

    mats = {m["code"]: m for m in _lm(active_only=False)}
    res = recommend(n_exploit=n_exploit, n_explore=n_explore, p_min=p_min, mats=mats)
    rec = res["recommendations"]
    cols = ["type", "cost", "p_pass", "dist", "min_margin"] + [c for c in D.COMP_COLS if c in rec and rec[c].abs().sum() > 0] + \
           [c for c in rec.columns if c.endswith("_mean")]
    return (res.get("note", "") + "\n" + rec[cols].round(3).to_string()), []


@tool("save_review", "保存复盘纪要到 08_复盘（Markdown）。", {"title": {"type": "string"}, "content": {"type": "string"}}, ["title", "content"])
def save_review(title: str, content: str):
    from ..config import KB

    path = KB["review"] / f"{db.now()[:10]}_{title[:40].replace('/', '_')}.md"
    kb.write_text(path, f"# {title}\n\n{content}\n")
    return f"已保存：{path.name}", []
