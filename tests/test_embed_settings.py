"""向量检索也能随便换：地址 + 密钥 + 嵌入模型 + 重排模型；换了嵌入模型，旧向量不混用，重建后再用。"""
from __future__ import annotations

import json

import numpy as np
import pytest

from polysage import db, embedsig, envfile, llm, providers
from polysage.config import settings
from polysage.llm import embed as real_embed      # 收集测试时拿到的是真函数（之后夹具会把 llm.embed 换成假的）
from polysage.llm import rerank as real_rerank

DASHSCOPE = {"name": "阿里云百炼", "base_url": "https://dashscope.example/compatible-mode/v1/", "embed_model": "text-embedding-v4",
             "rerank_model": "", "api_key": "sk-dash-123456789", "batch": 10}


def test_embed_providers_add_edit_switch():
    pid = providers.embed_upsert(DASHSCOPE)
    p = providers.embed_get(pid)
    assert p["base_url"].endswith("/v1") and p["batch"] == 10
    providers.embed_upsert({**p, "api_key": "", "rerank_model": "gte-rerank"})
    assert providers.embed_get(pid)["api_key"] == "sk-dash-123456789"                 # 密钥留空不改
    providers.embed_set_active(pid)
    assert (settings.embed_name, settings.embed_model, settings.rerank_model, settings.embed_batch) == \
        ("阿里云百炼", "text-embedding-v4", "gte-rerank", 10)
    assert settings.embed_api_key == "sk-dash-123456789" and settings.embed_ready
    assert any(k.startswith("向量检索：阿里云百炼") and ok for k, ok in settings.status().items())
    with pytest.raises(ValueError):
        providers.embed_delete(pid)                                                    # 正在用的不能删
    providers.embed_set_active("siliconflow")
    providers.embed_delete(pid)
    assert providers.embed_get(pid) is None
    providers.embed_upsert({"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
                            "embed_model": "BAAI/bge-m3", "rerank_model": "BAAI/bge-reranker-v2-m3", "api_key": "sk-sf-777777"})
    assert "SILICONFLOW_API_KEY=sk-sf-777777" in envfile.ENV_PATH.read_text(encoding="utf-8")   # 硅基流动钥匙两边共用


def test_old_config_file_gets_embed_section(monkeypatch):
    path = providers._path()
    data = json.loads(path.read_text(encoding="utf-8"))
    for k in ("embed_providers", "embed_active"):
        data.pop(k)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setattr(settings, "sf_api_key", "fake-sf")
    p = providers.embed_active()
    assert p["id"] == "siliconflow" and p["embed_model"] == "BAAI/bge-m3" and p["api_key"] == ""
    assert settings.embed_api_key == "fake-sf"                                         # 没单独填就用 SILICONFLOW_API_KEY
    assert "embed_providers" in json.loads(path.read_text(encoding="utf-8"))           # 迁移写回了文件


def test_embed_and_rerank_use_active_provider(monkeypatch):
    providers.embed_set_active(providers.embed_upsert(DASHSCOPE))
    calls = []

    def fake_post(path, payload, timeout=300.0, retries=3, kind="chat"):
        calls.append((llm._base_url(kind) + path, llm._headers(kind)["Authorization"], payload))
        return {"data": [{"index": i, "embedding": [1.0, float(i), 0.0]} for i in range(len(payload["input"]))]}

    monkeypatch.setattr(llm, "_post", fake_post)
    vecs = real_embed([f"段落{i}" for i in range(23)])
    assert vecs.shape == (23, 3) and np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    assert [len(c[2]["input"]) for c in calls] == [10, 10, 3]                           # 百炼一次最多 10 条
    url, auth, payload = calls[0]
    assert url == "https://dashscope.example/compatible-mode/v1/embeddings"
    assert auth == "Bearer sk-dash-123456789" and payload["model"] == "text-embedding-v4"
    calls.clear()
    assert real_rerank("薄膜", ["a", "b"]) is None and not calls                        # 没配重排模型：不发请求


def test_switching_model_pauses_vectors_until_rebuild(monkeypatch, fake_llm):
    from polysage.ingest import embed_missing, ingest_text, rebuild_vectors
    from polysage.rag import index as rag_index

    ingest_text("茂金属 LLDPE 提高纵向拉断力，透光也更好。" * 3, title="换模型前", source_type="tds", credibility=2)
    assert embedsig.stored() == "bge-m3" and embedsig.matches()
    try:
        providers.embed_set_active(providers.embed_upsert(DASHSCOPE))
        assert embedsig.sig() == "text-embedding-v4" and not embedsig.matches()
        sid, _ = ingest_text("碳酸钙填充母粒只加在 B 层，降成本。" * 3, title="换模型后", source_type="tds", credibility=2)
        assert db.q1("SELECT COUNT(*) c FROM chunks WHERE source_id=? AND embedding IS NOT NULL", (sid,))["c"] == 0
        assert embed_missing() == 0                                                     # 不和旧向量混着补

        def boom(*a, **k):
            raise AssertionError("模型对不上时检索不该再算向量")

        fake_embed = llm.embed
        monkeypatch.setattr(llm, "embed", boom)
        rag_index.invalidate()
        assert any("碳酸钙" in h["text"] for h in rag_index.search("碳酸钙 填充", top_k=5))   # 照样能按关键词搜
        monkeypatch.setattr(llm, "embed", fake_embed)
        res = rebuild_vectors()
        assert res["left"] == 0 and res["model"] == "text-embedding-v4"
        assert embedsig.stored() == "text-embedding-v4" and embedsig.matches()
        assert db.q1("SELECT COUNT(*) c FROM chunks WHERE embedding IS NULL")["c"] == 0
    finally:
        embedsig.mark("bge-m3")                       # 别影响后面的测试（库是共用的）
        rag_index.invalidate()


MAAS = {"name": "通义 MaaS", "base_url": "https://maas.example.com/compatible-mode/v1", "embed_model": "qwen3.7-text-embedding",
        "rerank_model": "qwen3.7-text-rerank", "api_key": "sk-maas-123456789", "batch": 32}


def test_embed_batch_shrinks_to_service_limit(monkeypatch):
    pid = providers.embed_upsert(MAAS)
    providers.embed_set_active(pid)
    sizes = []

    def fake_post(path, payload, timeout=300.0, retries=3, kind="chat"):
        n = len(payload["input"])
        sizes.append(n)
        if n > 20:
            raise llm.LLMError('通义 MaaS（向量） 400: {"error":{"message":"<400> InternalError.Algo.InvalidParameter: Value error, '
                               f'batch size is invalid, it should not be larger than 20,cur text batch:{n}."}}}}')
        return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(n)]}

    monkeypatch.setattr(llm, "_post", fake_post)
    assert real_embed([f"t{i}" for i in range(45)]).shape == (45, 2)
    assert sizes == [32, 20, 20, 5]                                                    # 被拒一次后按上限 20 发
    assert providers.embed_get(pid)["batch"] == 20                                     # 记住了，下次直接 20
    sizes.clear()
    real_embed([f"t{i}" for i in range(21)])
    assert sizes == [20, 1]
    with pytest.raises(llm.LLMError):                                                  # 别的 400（比如模型名错）照常报错
        monkeypatch.setattr(llm, "_post", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("x（向量） 400: model not found")))
        real_embed(["a", "b"])


def test_rerank_finds_compatible_api_path(monkeypatch):
    pid = providers.embed_upsert(MAAS)
    providers.embed_set_active(pid)
    urls = []

    def fake_post(path, payload, timeout=300.0, retries=3, kind="chat"):
        urls.append(path)
        if path != "https://maas.example.com/compatible-api/v1/reranks":
            raise llm.LLMError("通义 MaaS（向量） 404: ")
        return {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}

    monkeypatch.setattr(llm, "_post", fake_post)
    assert real_rerank("薄膜", ["钢材", "LLDPE 薄膜"]) == [(1, 0.9), (0, 0.1)]
    assert urls == ["https://maas.example.com/compatible-mode/v1/rerank", "https://maas.example.com/compatible-api/v1/reranks"]
    assert providers.embed_get(pid)["rerank_url"] == "https://maas.example.com/compatible-api/v1/reranks"
    urls.clear()
    real_rerank("薄膜", ["钢材", "LLDPE 薄膜"])
    assert urls == ["https://maas.example.com/compatible-api/v1/reranks"]                # 记住了，直接用
    providers.embed_upsert({**providers.embed_get(pid), "api_key": ""})
    assert "rerank_url" not in providers.embed_get(pid)                                 # 改过配置就重新找
