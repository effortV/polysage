"""测试夹具：临时 POLYSAGE_HOME + 假 LLM + 假检索源（离线可跑）。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="polysage_test_"))
os.environ["POLYSAGE_HOME"] = str(_TMP)
os.environ.setdefault("SILICONFLOW_API_KEY", "")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from polysage import llm  # noqa: E402
from polysage.sources.base import SearchHit  # noqa: E402


class FakeLLM:
    """按提示词内容返回结构化假答案，用于离线验证流水线逻辑。"""

    def __init__(self):
        self.calls: list[str] = []

    def chat(self, messages, **kw):
        text = "\n".join(m.get("content", "") for m in messages)
        self.calls.append(text[:80])
        if kw.get("json_mode"):
            return llm.ChatResult(content=json.dumps(self._json(text), ensure_ascii=False))
        if "复盘纪要" in text:
            body = "## 哪些预测对了、哪些错了\n- 大部分一致 [S1]\n## 需要修改的材料卡结论\n- 无\n## 约束是否需要调整\n- 否\n## 下一轮推荐方向\n- 向 base 收缩\n## 需更新的卡片清单\n- 无"
        elif "现配方机理报告" in text:
            body = ("# 现配方机理报告\n## 1 各组分的作用\n| 组分 | 用量 | 对拉伸 | 对撕裂 | 对穿刺 | 对热封 | 其他作用 | 依据 |\n|---|---|---|---|---|---|---|---|\n"
                    "| LLDPE | 60 | 主要来源 | 主要来源 | 主要来源 | 重要 | 韧性骨架 | [S1] |\n"
                    "## 2 为什么是这个比例\n- LLDPE 提供韧性 [S1]\n- LDPE 稳泡 [S1]\n## 3 性能担当与成本担当\n- LLDPE 是性能担当，再生料是成本担当 [S1]\n"
                    "## 4 替代空间\n- 韧性来源：茂金属 LLDPE 可以更少用量替代 [S1]\n- 成本填充：再生线性料\n## 5 待验证事项\n- 再生料批次波动")
        elif "当前状态摘要" in text:
            body = "已确认的约束与事实：合计 100%。已排除：无。待办：询价。未决：40% 比例。"
        elif "只依据资料片段回答" in text or "只依据给出的资料" in text:
            body = "- LLDPE 提供韧性与穿刺强度 [S1]\n- 加入 LDPE 提升膜泡稳定性与光学 [S1]"
        else:
            body = "OK [S1]"
        return llm.ChatResult(content=body)

    def _json(self, text: str):
        if "逐个评估下面的候选配方" in text:
            idxs = [int(x) for x in re.findall(r"^(\d+)\. \[", text, flags=re.M)]
            return {"items": [{"index": idx, "effects": {"拉伸": "≈", "撕裂": "≈/↑", "穿刺": "↑", "热封": "↑"},
                               "expected": "拉伸持平，穿刺热封略升", "risks": "压力", "risk_level": "中", "pass_confidence": ["高", "中", "低"][i % 3],
                               "evidence": "材料卡：茂金属 LLDPE"} for i, idx in enumerate(idxs)]}
        if "\"queries\"" in text:
            return {"queries": ["metallocene LLDPE blend film", "LLDPE 共混 吹膜"]}
        if "判断是否与" in text:
            n = len(re.findall(r"^\d+\. ", text, flags=re.M))
            return {"items": [{"index": i + 1, "relevant": i % 3 != 2, "kind": "material", "value": "有用"} for i in range(n)]}
        if "文献卡" in text and "citation" in text:
            return {"citation": "Fake et al. 2020", "system": "LLDPE/LDPE 吹膜", "conclusions": ["mLLDPE 提升落镖（定量）"],
                    "data_points": ["落镖 +80%"], "credibility": 3, "relevance": "相关"}
        if "专利卡" in text:
            return {"citation": "CN000", "applicant": "某公司", "formulation_range": "LLDPE 40-70", "claimed_effect": "韧性",
                    "borrow": ["茂金属 10-15%"], "avoid": ["权利要求 1"], "credibility": 4}
        if "材料卡" in text and "effects" in text:
            m = re.search(r"材料【(.+?)】", text)
            name = m.group(1) if m else "材料"
            return {"name": name, "grade_examples": ["示例"], "mfr": "1.0", "density": "0.918", "comonomer": "己烯",
                    "role": "韧性", "effects": {"拉伸": "≈ [S1]", "撕裂": "↑ [S1]", "穿刺": "↑↑ [S1]", "热封": "↑ [S1]"},
                    "typical_dosage": "10-20", "price_estimate": "待查", "risk": "压力升高", "evidence": "[S1]"}
        if "提取材料" in text:
            if "C4 LLDPE" in text.split("材料【", 1)[-1][:40]:
                return {"found": True, "price_rmb_per_ton": 8350, "date": "2026-09", "basis": "华东", "quote": "8350 元/吨"}
            return {"found": False}
        if "\"materials\"" in text and "补充最多" in text:
            return {"materials": [{"code": "LL8", "name": "C8 齐格勒 LLDPE", "category": "主体树脂", "role": "韧性", "typical_min": 10, "typical_max": 30, "why": "测试"}]}
        if "\"picks\"" in text:
            return {"picks": [{"rank": r, "reason": "覆盖思路", "watch": "膜泡"} for r in (1, 2, 3, 4, 5, 6)], "advice": "工艺固定"}
        if "逐条复核" in text:
            n = len(re.findall(r"^\d+\. \[", text, flags=re.M))
            return {"items": [{"index": i + 1, "verdict": "试", "reason": "合理", "watch": "压力", "modification": ""} for i in range(n)], "summary": "继续"}
        return {}

    def embed(self, texts, batch=32):
        rng = np.random.default_rng(abs(hash(texts[0])) % 2**32)
        v = rng.normal(size=(len(texts), 16)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def rerank(self, query, docs, top_n=None):
        return None


@pytest.fixture
def fake_llm(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(llm, "chat", f.chat)
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: f._json("\n".join(m.get("content", "") for m in messages)))
    monkeypatch.setattr(llm, "embed", f.embed)
    monkeypatch.setattr(llm, "rerank", f.rerank)
    from polysage.config import settings

    monkeypatch.setattr(settings, "sf_api_key", "fake")
    return f


def _fake_hits(query: str, kind: str = "paper", n: int = 4) -> list[SearchHit]:
    hits = []
    for i in range(n):
        key = re.sub(r"\W+", "", query)[:12] + str(i)
        if kind == "patent":
            hits.append(SearchHit(provider="google_patents", external_id=f"CN{key}", title=f"专利 {query} {i}", source_type="patent",
                                  venue="某公司", year=2020 + i, url=f"https://patents.google.com/patent/CN{key}/en",
                                  abstract="一种聚乙烯包装膜组合物，含茂金属 LLDPE 10-15%。", credibility=4))
        elif kind == "web":
            hits.append(SearchHit(provider="duckduckgo", external_id=f"https://example.com/{key}", title=f"网页 {query} {i}", source_type="price",
                                  url=f"https://example.com/{key}", abstract="LLDPE 7042 华东市场价 8350 元/吨", credibility=5))
        else:
            hits.append(SearchHit(provider="openalex", external_id=f"W{key}", title=f"Paper on {query} {i}", source_type="paper",
                                  authors="A, B", year=2015 + i, venue="J. Film", doi=f"10.1000/{key}", url=f"https://doi.org/10.1000/{key}",
                                  abstract="Metallocene LLDPE improves dart impact and seal strength of blown films; LDPE improves bubble stability.",
                                  credibility=3))
    return hits


@pytest.fixture
def fake_sources(monkeypatch):
    import polysage.pipeline.stage_collect as SC
    import polysage.pipeline.stage_mechanism as SM
    import polysage.pipeline.stage_scout as SS
    import polysage.ingest.pipeline as IP

    def search_all(query, providers=None, limit=20, year_from=None):
        providers = providers or ["openalex"]
        kind = "patent" if "google_patents" in providers else ("web" if "web" in providers else "paper")
        return _fake_hits(query, kind, n=min(limit, 4)), {}

    monkeypatch.setattr(SC, "search_all", search_all)
    monkeypatch.setattr(SM, "search_all", search_all)
    monkeypatch.setattr(SS, "search_all", search_all)
    monkeypatch.setattr(SS, "web_search", lambda q, limit=6: _fake_hits(q, "web", 2))
    monkeypatch.setattr(SS, "fetch_url", lambda url: {"kind": "html", "text": "LLDPE 7042 华东市场价 8350 元/吨（2026-09-10）", "file_path": "", "title": "价格页"})
    # 入库时不真的抓全文
    monkeypatch.setattr(IP, "_try_fulltext", lambda hit: ("", "", "测试：跳过全文"))
    return search_all


@pytest.fixture(scope="session")
def home() -> Path:
    return _TMP


def seed_library() -> int:
    """测试用：把早期首版 Top 20 当作示例配方写进临时库（正式库已不再自动写入这批种子）。"""
    from polysage.formulation import library as L

    n = 0
    for f in L.SEED_TOP20:
        if L.db.q1("SELECT id FROM formulations WHERE code=?", (f["code"],)):
            continue
        L.save_formulation(f["code"], f["comps"], structure=f["structure"], predicted={"effects_short": f["effects"]},
                           rationale=f["rationale"], risks=f["risks"], priority=f["priority"], status="候选", origin="测试示例",
                           require_buyable=False)     # 测试示例配方跳过“买得到”校验
        n += 1
    return n
