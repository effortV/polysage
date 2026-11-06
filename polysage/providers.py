"""对话模型服务商：想用哪家就填哪家（名称 + 接口地址 + 密钥 + 模型），选一个当前用，其余自动当备用。

向量检索（嵌入 + 重排）也一样：填接口地址、密钥、嵌入模型、重排模型，选一家当前用。
换了嵌入模型，旧向量就对不上了，要在设置页“重建向量索引”（见 polysage/embedsig.py）；重建前语义搜索自动停用、只用关键词。

存在 data/llm_providers.json（data/ 不进仓库）。第一次启动时从 .env 里旧的配置迁移过来。
硅基流动的密钥两边共用：哪边改了都会同步到 SILICONFLOW_API_KEY，另一边没单独填就用它。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

PRESETS: list[dict[str, str]] = [
    {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V4-Pro", "thinking": "enable_thinking"},
    {"name": "阿里云百炼（通义）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "thinking": "enable_thinking"},
    {"name": "DeepSeek 官方", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "thinking": "none"},
    {"name": "智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus", "thinking": "none"},
    {"name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2", "thinking": "none"},
    {"name": "自定义", "base_url": "", "model": "", "thinking": "none"},
]
# 向量检索：OpenAI 兼容的 /embeddings；重排走 /rerank（硅基流动、Jina 的格式），没有就留空、只做混合检索不重排
EMBED_PRESETS: list[dict[str, Any]] = [
    {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "embed_model": "BAAI/bge-m3",
     "rerank_model": "BAAI/bge-reranker-v2-m3", "batch": 32},
    {"name": "阿里云百炼（通义）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "embed_model": "text-embedding-v4",
     "rerank_model": "", "batch": 10},
    {"name": "智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4", "embed_model": "embedding-3", "rerank_model": "", "batch": 32},
    {"name": "Jina", "base_url": "https://api.jina.ai/v1", "embed_model": "jina-embeddings-v3",
     "rerank_model": "jina-reranker-v2-base-multilingual", "batch": 32},
    {"name": "自定义", "base_url": "", "embed_model": "", "rerank_model": "", "batch": 16},
]
THINKING_STYLES = {
    "none": "不发思考参数（最稳，不确定就选这个）",
    "enable_thinking": "enable_thinking（硅基流动、阿里云百炼）",
    "chat_template_kwargs": "chat_template_kwargs（自己用 vLLM 部署的模型）",
}


def _path():
    from .config import DATA_DIR

    return DATA_DIR / "llm_providers.json"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _guess_name(url: str) -> str:
    u = (url or "").lower()
    for key, name in (("siliconflow", "硅基流动"), ("qianwen", "通义千问"), ("dashscope", "阿里云百炼（通义）"),
                      ("zjumembrane", "浙大膜中心"), ("deepseek.com", "DeepSeek 官方"), ("bigmodel", "智谱"),
                      ("moonshot", "Kimi（月之暗面）")):
        if key in u:
            return name
    return "备用服务"


def _migrate() -> dict[str, Any]:
    """从 .env 里旧的两套配置生成服务商列表（行为保持不变：旧 ZJU 槽位的思考参数照旧）。"""
    providers = [{"id": "siliconflow", "name": "硅基流动",
                  "base_url": _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
                  "api_key": _env("SILICONFLOW_API_KEY"),
                  "model": _env("SILICONFLOW_CHAT_MODEL", "deepseek-ai/DeepSeek-V4-Pro"), "thinking": "enable_thinking"}]
    if _env("ZJU_API_KEY") or _env("ZJU_BASE_URL"):
        think = _env("ZJU_SUPPORTS_THINKING", "0") in ("1", "true", "True")
        url = _env("ZJU_BASE_URL", "https://api.zjumembrane.cn/v1")
        providers.append({"id": "zju", "name": _guess_name(url), "base_url": url, "api_key": _env("ZJU_API_KEY"),
                          "model": _env("ZJU_CHAT_MODEL", "zju-qwen"),
                          "thinking": "chat_template_kwargs" if think else "none"})
    active = _env("LLM_PROFILE", "siliconflow")
    if active not in {p["id"] for p in providers}:
        active = providers[0]["id"]
    return {"providers": providers, "active": active}


def _migrate_embed() -> dict[str, Any]:
    """向量检索原来固定用硅基流动：迁移成一家“硅基流动”，密钥留空 = 共用 SILICONFLOW_API_KEY。"""
    return {"embed_providers": [{"id": "siliconflow", "name": "硅基流动",
                                 "base_url": _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/"),
                                 "api_key": "", "embed_model": _env("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"),
                                 "rerank_model": _env("SILICONFLOW_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"), "batch": 32}],
            "embed_active": "siliconflow"}


def load() -> dict[str, Any]:
    p = _path()
    data = None
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not (isinstance(data.get("providers"), list) and data["providers"]):
                data = None
        except (OSError, ValueError):
            data = None
    changed = data is None
    if data is None:
        data = _migrate()
    if not (isinstance(data.get("embed_providers"), list) and data["embed_providers"]):
        data.update(_migrate_embed())
        changed = True
    if changed:
        save(data)
    return data


def save(data: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def all_providers() -> list[dict[str, Any]]:
    return list(load()["providers"])


def get(pid: str | None) -> dict[str, Any] | None:
    return next((p for p in load()["providers"] if p["id"] == pid), None)


def active_id() -> str:
    return load().get("active") or all_providers()[0]["id"]


def resolve(pid: str | None) -> dict[str, Any]:
    """settings.llm_profile → 服务商；找不到就用当前生效的那家。"""
    return get(pid) or get(active_id()) or {}


def effective_key(p: dict[str, Any] | None) -> str:
    """这家实际用的密钥：硅基流动那条没单独填时，用向量检索那把（SILICONFLOW_API_KEY，两边本来同步）。"""
    if not p:
        return ""
    if p.get("api_key"):
        return p["api_key"]
    if "siliconflow" in (p.get("base_url") or ""):
        from .config import settings

        return settings.sf_api_key
    return ""


def ready(p: dict[str, Any] | None) -> bool:
    return bool(p and effective_key(p) and p.get("base_url") and p.get("model"))


def set_active(pid: str) -> None:
    from .envfile import set_env

    data = load()
    if not any(p["id"] == pid for p in data["providers"]):
        raise KeyError(pid)
    data["active"] = pid
    save(data)
    set_env({"LLM_PROFILE": pid})          # 重启后也保持；同时刷新 settings.llm_profile


def upsert(p: dict[str, Any]) -> str:
    """新增或修改一家；密钥留空 = 不改原来的。返回 id。"""
    data = load()
    pid = p.get("id") or ""
    cur = next((x for x in data["providers"] if x["id"] == pid), None) if pid else None
    item = {"id": pid or ("p" + uuid.uuid4().hex[:6]), "name": (p.get("name") or "").strip() or _guess_name(p.get("base_url", "")),
            "base_url": (p.get("base_url") or "").strip().rstrip("/"), "model": (p.get("model") or "").strip(),
            "thinking": p.get("thinking") if p.get("thinking") in THINKING_STYLES else "none",
            "api_key": (p.get("api_key") or "").strip() or (cur or {}).get("api_key", "")}
    if cur:
        data["providers"] = [item if x["id"] == pid else x for x in data["providers"]]
    else:
        data["providers"].append(item)
    save(data)
    if "siliconflow" in item["base_url"] and (p.get("api_key") or "").strip():
        from .envfile import set_env

        set_env({"SILICONFLOW_API_KEY": item["api_key"]})      # 向量检索同步用这把钥匙
    return item["id"]


def delete(pid: str) -> None:
    data = load()
    if pid == data.get("active"):
        raise ValueError("当前正在用的服务商不能删，先切换到别的")
    data["providers"] = [x for x in data["providers"] if x["id"] != pid]
    save(data)


def fallback(pid: str | None) -> str | None:
    """备用：除当前外第一家配齐了的。"""
    return next((p["id"] for p in load()["providers"] if p["id"] != pid and ready(p)), None)


# ---------------- 向量检索服务商 ----------------
def embed_providers() -> list[dict[str, Any]]:
    return list(load()["embed_providers"])


def embed_get(pid: str | None) -> dict[str, Any] | None:
    return next((p for p in load()["embed_providers"] if p["id"] == pid), None)


def embed_active() -> dict[str, Any]:
    data = load()
    return embed_get(data.get("embed_active")) or data["embed_providers"][0]


def embed_ready(p: dict[str, Any] | None) -> bool:
    return bool(p and effective_key(p) and p.get("base_url") and p.get("embed_model"))


def embed_set_active(pid: str) -> None:
    data = load()
    if not any(p["id"] == pid for p in data["embed_providers"]):
        raise KeyError(pid)
    data["embed_active"] = pid
    save(data)


def embed_upsert(p: dict[str, Any]) -> str:
    """新增或修改一家向量检索服务；密钥留空 = 不改原来的。返回 id。"""
    data = load()
    pid = p.get("id") or ""
    cur = next((x for x in data["embed_providers"] if x["id"] == pid), None) if pid else None
    try:
        batch = max(1, min(int(p.get("batch") or 16), 256))
    except (TypeError, ValueError):
        batch = 16
    item = {"id": pid or ("e" + uuid.uuid4().hex[:6]),
            "name": (p.get("name") or "").strip() or _guess_name(p.get("base_url", "")),
            "base_url": (p.get("base_url") or "").strip().rstrip("/"),
            "embed_model": (p.get("embed_model") or "").strip(), "rerank_model": (p.get("rerank_model") or "").strip(),
            "batch": batch, "api_key": (p.get("api_key") or "").strip() or (cur or {}).get("api_key", "")}
    if cur:
        data["embed_providers"] = [item if x["id"] == pid else x for x in data["embed_providers"]]
    else:
        data["embed_providers"].append(item)
    save(data)
    if "siliconflow" in item["base_url"] and (p.get("api_key") or "").strip():
        from .envfile import set_env

        set_env({"SILICONFLOW_API_KEY": item["api_key"]})      # 硅基流动的钥匙两边共用
    return item["id"]


def embed_delete(pid: str) -> None:
    data = load()
    if pid == data.get("embed_active"):
        raise ValueError("当前正在用的向量检索服务不能删，先切换到别的")
    data["embed_providers"] = [x for x in data["embed_providers"] if x["id"] != pid]
    save(data)


def masked(key: str) -> str:
    key = key or ""
    if len(key) <= 8:
        return "已填" if key else "未填"
    return f"{key[:3]}…{key[-4:]}"


def slug_ok(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+", url or ""))
