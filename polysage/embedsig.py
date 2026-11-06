"""知识库向量是用哪个嵌入模型建的。

不同嵌入模型的向量不能混着比：换了模型以后，旧向量和新问题的向量对不上，语义搜索会乱排。
所以记一个“签名”（嵌入模型名）。签名和当前设置不一致时：
- 检索只用关键词（BM25），不用旧向量；
- 新入库的切片先不建向量（免得新旧混在一起）；
- 设置页点“重建向量索引”，清掉旧向量、用新模型全部重建。
"""
from __future__ import annotations

from typing import Any

from . import db
from .config import settings

KEY = "embed_signature"
LEGACY = "bge-m3"        # 早期没记签名时，向量都是硅基流动 BAAI/bge-m3 建的


def sig(model: str | None = None) -> str:
    """模型签名：取最后一段、小写（BAAI/bge-m3 与自建的 bge-m3 算同一个）。"""
    m = (settings.embed_model if model is None else model) or ""
    return m.strip().split("/")[-1].lower()


def _has_vectors() -> bool:
    return db.q1("SELECT 1 x FROM chunks WHERE embedding IS NOT NULL LIMIT 1") is not None


def stored() -> str:
    """现有向量的签名；一条向量都没有时为空。"""
    if not _has_vectors():
        return ""
    r = db.q1("SELECT value FROM kv WHERE key=?", (KEY,))
    return (r or {}).get("value") or LEGACY


def mark(value: str | None = None) -> None:
    db.execute("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (KEY, value if value is not None else sig(), db.now()))


def matches() -> bool:
    """现有向量能不能和当前嵌入模型一起用（没有向量也算能）。"""
    s = stored()
    return not s or s == sig()


def state() -> dict[str, Any]:
    r = db.q1("SELECT COUNT(*) n, SUM(embedding IS NOT NULL) e FROM chunks") or {}
    return {"chunks": int(r.get("n") or 0), "embedded": int(r.get("e") or 0), "stored": stored(), "current": sig(),
            "matches": matches(), "ready": settings.embed_ready}
