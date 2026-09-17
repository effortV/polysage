"""混合检索索引：BM25（jieba 分词，中英通用）+ 向量（bge-m3）+ RRF 融合 + 可选重排。

索引在内存中缓存，按 chunks 表的 (count, max id) 版本号自动刷新。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import db, llm

_lock = threading.RLock()


def tokenize(text: str) -> list[str]:
    import jieba

    text = text.lower()
    toks: list[str] = []
    for t in jieba.cut_for_search(text):
        t = t.strip()
        if not t or re.fullmatch(r"[\s\W_]+", t):
            continue
        toks.append(t)
    # 追加英文/数字 token（如 mLLDPE、0.918、C6）
    toks.extend(re.findall(r"[a-z0-9][a-z0-9.\-]{1,}", text))
    return toks


class BM25:
    """Lucene 风格 BM25（idf 恒正），小语料下也有区分度。"""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lens = np.array([len(d) for d in docs], dtype=float)
        self.avg = float(self.lens.mean()) if self.n else 1.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for d in docs:
            c: dict[str, int] = {}
            for t in d:
                c[t] = c.get(t, 0) + 1
            self.tf.append(c)
            for t in c:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: float(np.log(1.0 + (self.n - n + 0.5) / (n + 0.5))) for t, n in df.items()}

    def get_scores(self, query: list[str]) -> np.ndarray:
        scores = np.zeros(self.n)
        for t in set(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, c in enumerate(self.tf):
                f = c.get(t)
                if f:
                    scores[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
        return scores


@dataclass
class Index:
    version: tuple[int, int] = (0, 0)
    chunk_ids: list[int] = field(default_factory=list)
    source_ids: list[int] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    bm25: Any = None
    emb: np.ndarray | None = None          # (n, d) 仅含有向量的块
    emb_rows: np.ndarray | None = None     # 对应 texts 的行号


_index: Index | None = None


def _version() -> tuple[int, int]:
    r = db.q1("SELECT COUNT(*) c, COALESCE(MAX(id),0) m, SUM(embedding IS NOT NULL) e FROM chunks")
    return (int(r["c"]), int(r["m"]) * 1000 + int(r["e"] or 0))


def get_index(force: bool = False) -> Index:
    global _index
    with _lock:
        v = _version()
        if _index is not None and _index.version == v and not force:
            return _index
        rows = db.q("SELECT id, source_id, text, embedding FROM chunks ORDER BY id")
        idx = Index(version=v)
        idx.chunk_ids = [r["id"] for r in rows]
        idx.source_ids = [r["source_id"] for r in rows]
        idx.texts = [r["text"] for r in rows]
        if rows:
            idx.bm25 = BM25([tokenize(t) for t in idx.texts])
            embs, erows = [], []
            for i, r in enumerate(rows):
                if r["embedding"]:
                    embs.append(np.frombuffer(r["embedding"], dtype=np.float32))
                    erows.append(i)
            if embs and len({e.shape[0] for e in embs}) == 1:
                idx.emb = np.vstack(embs)
                idx.emb_rows = np.asarray(erows)
        _index = idx
        return idx


def invalidate() -> None:
    global _index
    with _lock:
        _index = None


def search(query: str, top_k: int = 8, source_types: list[str] | None = None, use_rerank: bool = True,
           candidate_k: int = 40) -> list[dict[str, Any]]:
    """返回 [{chunk_id, source_id, text, score, source}]。"""
    idx = get_index()
    if not idx.texts:
        return []
    n = len(idx.texts)
    ranks: dict[int, float] = {}

    # BM25
    bm = idx.bm25.get_scores(tokenize(query))
    for rank, i in enumerate(np.argsort(-bm)[:candidate_k]):
        if bm[i] > 0:
            ranks[int(i)] = ranks.get(int(i), 0.0) + 1.0 / (60 + rank)

    # 向量
    if idx.emb is not None:
        qv = llm.embed([query])
        if qv is not None:
            sims = idx.emb @ qv[0]
            for rank, j in enumerate(np.argsort(-sims)[:candidate_k]):
                i = int(idx.emb_rows[j])
                ranks[i] = ranks.get(i, 0.0) + 1.0 / (60 + rank)

    if not ranks:
        return []
    cand = sorted(ranks.items(), key=lambda kv: -kv[1])[:candidate_k]

    # 来源过滤
    src_ids = list({idx.source_ids[i] for i, _ in cand})
    marks = ",".join("?" for _ in src_ids)
    srows = {r["id"]: r for r in db.q(f"SELECT * FROM sources WHERE id IN ({marks})", tuple(src_ids))}
    if source_types:
        cand = [(i, s) for i, s in cand if srows.get(idx.source_ids[i], {}).get("source_type") in source_types]

    # 重排
    if use_rerank and cand:
        rr = llm.rerank(query, [idx.texts[i] for i, _ in cand], top_n=min(top_k * 2, len(cand)))
        if rr:
            cand = [(cand[j][0], score) for j, score in rr]

    out = []
    for i, score in cand[:top_k]:
        out.append({"chunk_id": idx.chunk_ids[i], "source_id": idx.source_ids[i], "text": idx.texts[i],
                    "score": float(score), "source": srows.get(idx.source_ids[i], {})})
    return out
